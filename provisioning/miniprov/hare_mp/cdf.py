import os.path as P
import subprocess as S
from dataclasses import dataclass
from string import Template
from typing import Iterator, List, Optional, Tuple
from math import floor, ceil
import pkg_resources
from urllib.parse import urlparse
from enum import Enum, auto

from cortx.utils.cortx import Const
from hare_mp.store import ValueProvider
from hare_mp.types import (ClusterDesc, DiskRef, DList, Maybe, NodeDesc,
                           PoolDesc, PoolType, ProfileDesc, Protocol, Text,
                           M0ServerDesc, DisksDesc, AllowedFailures, Layout,
                           FdmiFilterDesc, NetworkPorts, M0ClientDesc,
                           ClientPort, ServerPort)
from hare_mp.utils import func_log, func_enter, func_leave, Utils

DHALL_PATH = '/opt/seagate/cortx/hare/share/cfgen/dhall'
DHALL_EXE = '/opt/seagate/cortx/hare/bin/dhall'
DHALL_TO_YAML_EXE = '/opt/seagate/cortx/hare/bin/dhall-to-yaml'


class procT(Enum):

    """Motr service type"""
    M0_CST_MDS = 1
    M0_CST_IOS = auto()
    M0_CST_CONFD = auto()
    M0_CST_RMS = auto()
    M0_CST_STATS = auto()
    M0_CST_HA = auto()
    M0_CST_SSS = auto()
    M0_CST_SNS_REP = auto()
    M0_CST_SNS_REB = auto()
    M0_CST_ADDB2 = auto()
    M0_CST_CAS = auto()
    M0_CST_DIX_REP = auto()
    M0_CST_DIX_REB = auto()
    M0_CST_DS1 = auto()
    M0_CST_DS2 = auto()
    M0_CST_FIS = auto()
    M0_CST_FDMI = auto()
    M0_CST_BE = auto()
    M0_CST_M0T1FS = auto()
    M0_CST_CLIENT = auto()
    M0_CST_ISCS = auto()

    '''def SvcT_to_proc(stype: SvcT) -> str:
        svcT_to_proc = {
            M0_CST_MDS: 'mds',
            M0_CST_IOS: 'ios',
            M0_CST_CONFD: 'confd',
            M0_CST_RMS: 'rms',
            M0_CST_STATS: 'stats',
            M0_CST_HA: 'ha',
            M0_CST_SSS: 'sss',
            M0_CST_SNS_REP: 'sns_repair',
            M0_CST_SNS_REB: 'sns_rebalance',
            M0_CST_ADDB2: 'addb',
            M0_CST_CAS: 'cas'}'''

    def __repr__(self):
        return self.name


@dataclass
class PoolHandle:
    cluster_id: str
    pool_type: str
    storage_ndx: int

    def tuple(self) -> Tuple[str, str, int]:
        # We could have used dataclasses.astuple() instead here but explicit
        # implementation is safer (we can be sure that the method is not
        # broken after somebody has changed the order of fields)
        return (self.cluster_id, self.pool_type, self.storage_ndx)


class CdfGenerator:
    def __init__(self,
                 provider: ValueProvider):
        super().__init__()
        self.provider = provider
        self.utils = Utils(provider)

    def _get_dhall_path(self) -> str:
        if P.exists(DHALL_PATH):
            return DHALL_PATH
        raise RuntimeError('CFGEN Dhall types not found')

    def _gencdf(self) -> str:
        resource_path = 'dhall/gencdf.dhall'
        raw_content: bytes = pkg_resources.resource_string(
            'hare_mp', resource_path)
        return raw_content.decode('utf-8')

    # cluster>id
    def _get_cluster_id(self) -> str:
        conf = self.provider
        cluster_id = conf.get('cluster>id')
        return cluster_id

    def _create_node_descriptions(self) -> List[NodeDesc]:
        nodes: List[NodeDesc] = []
        conf = self.provider
        # Skipping for controller and HA pod
        machines = conf.get_machine_ids_for_service(
            Const.SERVICE_MOTR_IO.value)
        # Get all the pods which runs the client components
        # cortx>motr>num_clients
        # cortx>motr>clients[N]>name
        client_machines = []
        num_clients = int(conf.get('cortx>motr>num_clients'))
        for i in range(num_clients):
            if int(conf.get(f'cortx>motr>clients[{i}]>num_instances')) > 0:
                name = conf.get(f'cortx>motr>clients[{i}]>name')
                client_machines.extend(conf.get_machine_ids_for_service(name))
                client_machines.extend(
                    conf.get_machine_ids_for_component(name))

        # Avoid adding duplicate machine ids if client and data node
        # are the same. We do not use list(set()) mechanism as it
        # changes the order and since this code is executed on all
        # the nodes in-parallel, the configuration generated on
        # every node must follow the same order to maintain consistency.
        for machine in client_machines:
            if machine not in machines:
                machines.append(machine)

        is_node_group_supported: bool = False
        local_node_group = self.utils.get_node_group(conf.get_machine_id(),
                                                     allow_null=True)

        if local_node_group:
            is_node_group_supported = True
        for machine in machines:
            nodes.append(self._create_node(machine, is_node_group_supported))
        return nodes

    # cluster>storage_set[N]>durability>{type}>data/parity/spare
    def _get_pool_property(self, pool: PoolHandle, prop_name: str) -> int:
        conf = self.provider
        (cluster_id, pool_type, storage_ndx) = pool.tuple()

        return int(
            conf.get(f'cluster>storage_set[{storage_ndx}]>'
                     f'durability>{pool_type}>{prop_name}'))

    def _get_layout(self, pool: PoolHandle) -> Optional[Layout]:
        (cluster_id, pool_type, storage_ndx) = pool.tuple()

        def prop(name: str) -> int:
            return self._get_pool_property(pool, name)

        return Layout(data=prop('data'),
                      spare=prop('spare'),
                      parity=prop('parity'))

    def _get_devices(self, pool: PoolHandle, node: str) -> List[str]:
        conf = self.provider
        pool_type = pool.pool_type
        prop_name = 'data'
        # node>{machine-id}>num_cvg
        cvg_num = int(conf.get(f'node>{node}>num_cvg'))
        all_cvg_devices = []
        if pool_type == 'dix':
            prop_name = 'metadata'
        for i in range(cvg_num):
            prop_num = int(conf.get(
                f'node>{node}>cvg[{i}]>devices>num_{prop_name}'))
            for j in range(prop_num):
                # node>{machine-id}>cvg[N]>devices>data/metadata[N]
                all_cvg_devices.append(conf.get(
                    f'node>{node}>cvg[{i}]>devices>{prop_name}[{j}]'))
        return all_cvg_devices

    def _validate_pool(self, pool: PoolHandle) -> None:
        layout = self._get_layout(pool)
        if not layout:
            return

        conf = self.provider
        (cluster_id, pool_type, i) = pool.tuple()

        # cluster>storage_set[N]>name
        storage_set_name = conf.get(
            f'cluster>storage_set[{i}]>name')

        data_devices_count: int = 0
        data_nodes = conf.get_machine_ids_for_service(
            Const.SERVICE_MOTR_IO.value)

        for node in data_nodes:
            data_devices_count += len(self._get_devices(pool, node))

        (data, parity, spare) = (layout.data, layout.parity, layout.spare)
        min_width = data + parity + spare
        if data_devices_count and (data_devices_count < min_width):
            raise RuntimeError(
                'Invalid storage set configuration '
                f'(name={storage_set_name}):'
                f'device count ({data_devices_count}) must be not '
                f'less than N+K+S ({min_width})')

    # Current formula is as follows
    # Disk or device failure = K(parity)
    # Node failures = floor(K/ceil((N+K+S)/ number of nodes))
    # Controller or CVG failure = min(K, cvg per node * Node failures)
    def _calculate_allowed_failure(self, layout: Layout) -> AllowedFailures:
        conf = self.provider
        machine_id = conf.get_machine_id()
        # This is a workaround for getting cvg by looking at one data node
        # only. The implementation here needs to be corrected using a
        # different task (EOS-27063).
        data_nodes = conf.get_machine_ids_for_service(
            Const.SERVICE_MOTR_IO.value)

        node_count = len(data_nodes)
        machine_id = data_nodes[0]
        # node>{machine-id}>num_cvg
        cvg_per_node = int(conf.get(
            f'node>{machine_id}>num_cvg'))

        total_unit = layout.data + layout.parity + layout.spare
        if total_unit == 0:
            raise RuntimeError('All layout parameters are 0')

        enc_failure_allowed_FP = layout.parity / ceil(total_unit / node_count)
        enc_failure_allowed = floor(enc_failure_allowed_FP)

        temp = cvg_per_node * enc_failure_allowed
        ctrl_failure_allowed = min(temp, layout.parity)

        return AllowedFailures(site=0,
                               rack=0,
                               encl=enc_failure_allowed,
                               ctrl=ctrl_failure_allowed,
                               disk=layout.parity)

    def _add_pool(self, pool: PoolHandle, out_list: List[PoolDesc]) -> None:
        conf = self.provider
        layout = self._get_layout(pool)
        if not layout:
            return
        (cid, pool_type, i) = pool.tuple()
        storage_set_name = conf.get(f'cluster>storage_set[{i}]>name')
        pool_name = f'{storage_set_name}__{pool_type}'
        allowed_failure = self._calculate_allowed_failure(layout)
        data_nodes = conf.get_machine_ids_for_service(
            Const.SERVICE_MOTR_IO.value)

        out_list.append(
            PoolDesc(
                name=Text(pool_name),
                disk_refs=Maybe(
                    DList([
                        DiskRef(path=Text(device),
                                node=Maybe(Text(self.utils.get_hostname(node)),
                                           'Text'))
                        for node in data_nodes
                        for device in self._get_devices(pool, node)
                    ], 'List DiskRef'), 'List DiskRef'),
                data_units=layout.data,
                parity_units=layout.parity,
                spare_units=Maybe(layout.spare, 'Natural'),
                type=PoolType[pool_type],
                allowed_failures=Maybe(allowed_failure, 'AllowedFailures')))

    def _create_pool_descriptions(self) -> List[PoolDesc]:
        pools: List[PoolDesc] = []
        conf = self.provider
        cluster_id = self._get_cluster_id()
        # cluster>num_storage_set
        num_storage_set = int(
            conf.get('cluster>num_storage_set'))

        for i in range(num_storage_set):
            for pool_type in ('sns', 'dix'):
                handle = PoolHandle(cluster_id=cluster_id,
                                    pool_type=pool_type,
                                    storage_ndx=i)
                self._validate_pool(handle)
                self._add_pool(handle, pools)

        return pools

    def _create_profile_descriptions(
            self, pool_desc: List[PoolDesc]) -> List[ProfileDesc]:
        profiles: List[ProfileDesc] = []

        profiles.append(
            ProfileDesc(name=Text('Profile_the_pool'),
                        pools=DList([pool.name for pool in pool_desc],
                                    'List Text')))

        return profiles

    def _create_fdmi_filter_descriptions(
            self, nodes: List[NodeDesc]) -> Maybe[List[FdmiFilterDesc]]:
        return Maybe(None, 'List T.FdmiFilterDesc')

    def _create_ports_descriptions(self, hostname: str) -> NetworkPorts:
        conf = self.provider
        m0serverT = ['ios', 'confd']
        m0server_ports = []
        m0client_ports = []
        for srv in NetworkPorts.__annotations__.keys():
            if srv == 'hax':
                num_ep = int(conf.get('cortx>hare>hax>num_endpoints'))
                _hax = None
                _hax_http = None
                for i in range(num_ep):
                    url = conf.get(f'cortx>hare>hax>endpoints[{i}]')
                    _parsed_url = urlparse(url)
                    if _parsed_url.scheme in ('http', 'https'):
                        _hax_http = _parsed_url.port
                    else:
                        if _parsed_url.hostname == hostname:
                            _hax = round(_parsed_url.port / 100) * 100
            elif srv == 'm0_client_other':
                num_clients = int(conf.get('cortx>motr>num_clients'))
                for i in range(num_clients):
                    # Note: every client will not have endpoints
                    num_endpoints = conf.get(
                        f'cortx>motr>clients[{i}]>num_endpoints',
                        allow_null=True)
                    if num_endpoints:
                        for j in range(int(num_endpoints)):
                            url = conf.get(
                                f'cortx>motr>clients[{i}]>endpoints[{j}]')
                            _parsed_url = urlparse(url)
                            if _parsed_url.hostname == hostname:
                                port = round(_parsed_url.port / 100) * 100
                                client_name = conf.get(
                                    f'cortx>motr>clients[{i}]>name')
                                m0client_ports.append(
                                    ClientPort(name=Text(client_name),
                                               port=int(port)))
            elif srv == 'm0_server':
                for server in m0serverT:
                    num_ep = int(conf.get(
                        f'cortx>motr>{server}>num_endpoints'))
                    port = 0
                    for i in range(num_ep):
                        url = conf.get(f'cortx>motr>{server}>endpoints[{i}]')
                        _parsed_url = urlparse(url)
                        if _parsed_url.hostname == hostname:
                            port = round(_parsed_url.port / 100) * 100
                    m0server_ports.append(
                        ServerPort(name=Text(server),
                                   port=int(port)))
            else:
                _client_s3 = None

        return NetworkPorts(
            hax=Maybe(_hax, 'Natural'),
            hax_http=Maybe(_hax_http, 'Natural'),
            m0_server=Maybe(DList(m0server_ports, 'List ServerPort'),
                            'List ServerPort'),
            m0_client_other=Maybe(DList(m0client_ports, 'List ClientPort'),
                                  'List ClientPort'),
            m0_client_s3=Maybe(_client_s3, 'Natural'))

    def _get_cdf_dhall(self) -> str:
        dhall_path = self._get_dhall_path()
        conf = self.provider
        nodes = self._create_node_descriptions()
        pools = self._create_pool_descriptions()
        profiles = self._create_profile_descriptions(pools)
        fdmi_filters = self._create_fdmi_filter_descriptions(nodes)
        create_aux = conf.get('cluster>create_aux',
                              allow_null=True)

        if create_aux is None:
            create_aux = False

        params_text = str(
            ClusterDesc(create_aux=Maybe(create_aux, 'Bool'),
                        node_info=DList(nodes, 'List NodeInfo'),
                        pool_info=DList(pools, 'List PoolInfo'),
                        profile_info=DList(profiles, 'List ProfileInfo'),
                        fdmi_filter_info=fdmi_filters))

        gencdf = Template(self._gencdf()).substitute(path=dhall_path,
                                                     params=params_text)
        return gencdf

    @func_log(func_enter, func_leave)
    def generate(self) -> str:
        gencdf = self._get_cdf_dhall()

        dhall = S.Popen([DHALL_EXE],
                        stdin=S.PIPE,
                        stdout=S.PIPE,
                        stderr=S.PIPE,
                        encoding='utf8')

        dhall_out, err_d = dhall.communicate(input=gencdf)
        if dhall.returncode:
            raise RuntimeError(f'dhall binary failed: {err_d}')

        to_yaml = S.Popen([DHALL_TO_YAML_EXE],
                          stdin=S.PIPE,
                          stdout=S.PIPE,
                          stderr=S.PIPE,
                          encoding='utf8')

        yaml_out, err = to_yaml.communicate(input=dhall_out)
        if to_yaml.returncode:
            raise RuntimeError(f'dhall-to-yaml binary failed: {err}')
        return yaml_out

    # Only required for non K8s
    def _get_iface(self, machine_id: str) -> str:
        ifaces = self.provider.get(
            f'node>{machine_id}>network>data>private_interfaces',
            allow_null=True)
        if not ifaces:
            # In LC environment:
            # 1. This key will not be present
            # 2. The value is ignored by Motr anyway
            #
            # So we don't need to fail if the key is absent but return some
            # dummy value instead.
            return 'dummy'
        return ifaces[0]

    def _get_iface_type(self, machine_id: str) -> Optional[Protocol]:
        num_ep = int(self.provider.get('cortx>hare>hax>num_endpoints'))

        hostname = self.utils.get_hostname(machine_id)

        proto = None
        # Expected format '<protocol>://<hostname>:<port>'
        # e.g. endpoints:
        #      - tcp://data1-node1:22001  # For motr and Hax communication
        #      - tcp://data1-node2:22001  # For motr and Hax communication
        for i in range(num_ep):
            endpoint = self.provider.get(f'cortx>hare>hax>endpoints[{i}]')
            key = endpoint.split(':')

            if key[0] in ('http', 'https'):
                continue

            if key[1].split('/')[2] == hostname:
                proto = key[0]
                break

        if proto is None:
            return None
        return Protocol[proto]

    # node>{machine -id}>cvg[N]>devices>num_data
    # node>{machine -id}>cvg[N]>devices>data[N]
    def _get_data_devices(self, machine_id: str, cvg: int) -> DList[Text]:
        store = self.provider
        data_devices = []
        num_data = int(store.get(
            f'node>{machine_id}>cvg[{cvg}]>devices>num_data'))
        for i in range(num_data):
            data_devices.append(Text(store.get(
                f'node>{machine_id}>cvg[{cvg}]>devices>data[{i}]')))
        return DList(data_devices, 'List Text')

    # conf-store returns a list of devices, thus, the function
    # must return a single metadata device path instead of a string of
    # list.
    def _get_metadata_device(self,
                             machine_id: str,
                             cvg: int) -> Text:
        store = self.provider
        metadata_device = Text(store.get(
            f'node>{machine_id}>cvg[{cvg}]>devices>metadata[0]'))
        return metadata_device

    # This function is kept as place holder with length returning 1,
    # as policy needs to be decided for a commong solution that is
    # applicable for LR and LC. This function can be used or removed
    # in that task (EOS-26849)
    def _get_m0d_per_cvg(self, machine_id: str, cvg: int) -> int:
        length = 1
        return length

    def _get_node_clients(self, machine_id: str) -> Iterator[M0ClientDesc]:
        """
        Get motr client nodes.

        For all the motr clients present in the cluster return only those
        clients that are present in the components list for the given
        node>{machine_id} according to the ConfStore.

        cortx>motr>clients=[rgw_s3, other]
        node>{machine_id}>components=[rgw, other]

        return 'other' only.
        """
        conf = self.provider
        num_clients = int(conf.get('cortx>motr>num_clients'))
        for i in range(num_clients):
            no_instances = int(conf.get(
                f'cortx>motr>clients[{i}]>num_instances'))
            if no_instances > 0:
                name = str(conf.get(f'cortx>motr>clients[{i}]>name'))
                if self.utils.is_component_or_service(machine_id, name):
                    yield M0ClientDesc(
                        name=Text(name),
                        instances=no_instances)

    def _create_node(self, machine_id: str,
                     is_node_group_supported: bool) -> NodeDesc:
        store = self.provider

        hostname = self.utils.get_hostname(machine_id)
        node_group = None
        iface = self._get_iface(machine_id)
        servers = None
        if(self.utils.is_motr_io_present(machine_id)):
            if is_node_group_supported:
                node_group = self.utils.get_node_group(machine_id)
            # Currently, there is 1 m0d per cvg.
            # We will create 1 IO service entry in CDF per cvg.
            # An IO service entry will use data  and metadat devices
            # from corresponding cvg.
            servers = DList([
                M0ServerDesc(
                    io_disks=DisksDesc(
                        data=self.utils.get_data_drives_info_for(cvg,
                                                                 machine_id),
                        meta_data=Maybe(
                            self._get_metadata_device(
                                machine_id, cvg), 'Text'),
                        log=self.utils.get_log_drives_info_for(cvg,
                                                               machine_id)),
                    runs_confd=Maybe(False, 'Bool'))
                # node>{machine_id}>num_cvg
                # node>{machine_id}>cvg
                for cvg in range(int(store.get(
                    f'node>{machine_id}>num_cvg')))
                for m0d in range(self._get_m0d_per_cvg(machine_id, cvg))
            ], 'List M0ServerDesc')

            # Adding a Motr confd entry per server node in CDF.
            # The `runs_confd` value (true/false) determines
            # if Motr confd process will be started on the node or not.
            servers.value.append(M0ServerDesc(
                io_disks=DisksDesc(
                    data=DList([], 'List Disk'),
                    log=DList([], 'List Disk'),
                    meta_data=Maybe(None, 'Text')),
                runs_confd=Maybe(True, 'Bool')))

        # adding clients
        clients = DList([
            client
            for client in self._get_node_clients(machine_id)
        ], 'List M0ClientDesc')
        m0_clients = clients if clients else None

        node_facts = self.utils.get_node_facts()

        network_ports = self._create_ports_descriptions(hostname)

        return NodeDesc(
            hostname=Text(hostname),
            node_group=Maybe(Text(str(node_group)), 'Text'),
            machine_id=Maybe(Text(machine_id), 'Text'),
            processorcount=Maybe(node_facts['processorcount'], 'Natural'),
            memorysize_mb=Maybe(node_facts['memorysize_mb'], 'Double'),
            data_iface=Text(iface),
            data_iface_ip_addr=Maybe(Text(hostname), 'Text'),
            data_iface_type=Maybe(self._get_iface_type(machine_id), 'P'),
            transport_type=Text(self.utils.get_transport_type()),
            m0_servers=Maybe(servers, 'List M0ServerDesc'),
            m0_clients=Maybe(m0_clients, 'List M0ClientDesc'),
            ports_info=Maybe(network_ports, 'T.NetworkPorts')
        )
