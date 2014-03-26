# Copyright 2012 OpenStack Foundation
# All Rights Reserved
# Copyright (c) 2012 NEC Corporation
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# vim: tabstop=4 shiftwidth=4 softtabstop=4

import time

from oslo.config import cfg
from quantumclient.common import exceptions as qexceptions

from nova.compute import instance_types
from nova import conductor
from nova import context
from nova.db import base
from nova import exception
from nova.network import api as network_api
from nova.network import model as network_model
from nova.network import quantumv2
from nova.network.quantumv2 import constants
from nova.network.security_group import openstack_driver
from nova.openstack.common import excutils
from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging
from nova.openstack.common import uuidutils
from quantumclient.quantum import v2_0 as quantumv20

quantum_opts = [
    cfg.StrOpt('quantum_url',
               default='http://127.0.0.1:9696',
               help='URL for connecting to quantum'),
    cfg.IntOpt('quantum_url_timeout',
               default=30,
               help='timeout value for connecting to quantum in seconds'),
    cfg.StrOpt('quantum_admin_username',
               help='username for connecting to quantum in admin context'),
    cfg.StrOpt('quantum_admin_password',
               help='password for connecting to quantum in admin context',
               secret=True),
    cfg.StrOpt('quantum_admin_tenant_name',
               help='tenant name for connecting to quantum in admin context'),
    cfg.StrOpt('quantum_region_name',
               help='region name for connecting to quantum in admin context'),
    cfg.StrOpt('quantum_admin_auth_url',
               default='http://localhost:5000/v2.0',
               help='auth url for connecting to quantum in admin context'),
    cfg.BoolOpt('quantum_api_insecure',
                default=False,
                help='if set, ignore any SSL validation issues'),
    cfg.StrOpt('quantum_auth_strategy',
               default='keystone',
               help='auth strategy for connecting to '
                    'quantum in admin context'),
    # TODO(berrange) temporary hack until Quantum can pass over the
    # name of the OVS bridge it is configured with
    cfg.StrOpt('quantum_ovs_bridge',
               default='br-int',
               help='Name of Integration Bridge used by Open vSwitch'),
    cfg.IntOpt('quantum_extension_sync_interval',
                default=600,
                help='Number of seconds before querying quantum for'
                     ' extensions'),
    ]

CONF = cfg.CONF
CONF.register_opts(quantum_opts)
CONF.import_opt('default_floating_pool', 'nova.network.floating_ips')
CONF.import_opt('flat_injected', 'nova.network.manager')
LOG = logging.getLogger(__name__)

refresh_cache = network_api.refresh_cache
update_instance_info_cache = network_api.update_instance_cache_with_nw_info


class API(base.Base):
    """API for interacting with the quantum 2.x API."""

    conductor_api = conductor.API()
    security_group_api = openstack_driver.get_openstack_security_group_driver()

    def __init__(self):
        super(API, self).__init__()
        self.last_quantum_extension_sync = None
        self.extensions = {}

    def setup_networks_on_host(self, context, instance, host=None,
                               teardown=False):
        """Setup or teardown the network structures."""

    def _get_available_networks(self, context, project_id,
                                net_ids=None):
        """Return a network list available for the tenant.
        The list contains networks owned by the tenant and public networks.
        If net_ids specified, it searches networks with requested IDs only.
        """
        quantum = quantumv2.get_client(context)

        # If user has specified to attach instance only to specific
        # networks, add them to **search_opts
        # (1) Retrieve non-public network list owned by the tenant.
        search_opts = {"tenant_id": project_id, 'shared': False}
        if net_ids:
            search_opts['id'] = net_ids
        nets = quantum.list_networks(**search_opts).get('networks', [])
        # (2) Retrieve public network list.
        search_opts = {'shared': True}
        if net_ids:
            search_opts['id'] = net_ids
        nets += quantum.list_networks(**search_opts).get('networks', [])

        _ensure_requested_network_ordering(
            lambda x: x['id'],
            nets,
            net_ids)

        return nets

    def allocate_for_instance(self, context, instance, **kwargs):
        """Allocate network resources for the instance.

        TODO(someone): document the rest of these parameters.

        :param macs: None or a set of MAC addresses that the instance
            should use. macs is supplied by the hypervisor driver (contrast
            with requested_networks which is user supplied).
            NB: QuantumV2 currently assigns hypervisor supplied MAC addresses
            to arbitrary networks, which requires openflow switches to
            function correctly if more than one network is being used with
            the bare metal hypervisor (which is the only one known to limit
            MAC addresses).
        """
        hypervisor_macs = kwargs.get('macs', None)
        available_macs = None
        if hypervisor_macs is not None:
            # Make a copy we can mutate: records macs that have not been used
            # to create a port on a network. If we find a mac with a
            # pre-allocated port we also remove it from this set.
            available_macs = set(hypervisor_macs)
        quantum = quantumv2.get_client(context)
        LOG.debug(_('allocate_for_instance() for %s'),
                  instance['display_name'])
        if not instance['project_id']:
            msg = _('empty project id for instance %s')
            raise exception.InvalidInput(
                reason=msg % instance['display_name'])
        requested_networks = kwargs.get('requested_networks')
        ports = {}
        fixed_ips = {}
        net_ids = []
        if requested_networks:
            for network_id, fixed_ip, port_id in requested_networks:
                if port_id:
                    port = quantum.show_port(port_id)['port']
                    if hypervisor_macs is not None:
                        if port['mac_address'] not in hypervisor_macs:
                            raise exception.PortNotUsable(port_id=port_id,
                                instance=instance['display_name'])
                        else:
                            # Don't try to use this MAC if we need to create a
                            # port on the fly later. Identical MACs may be
                            # configured by users into multiple ports so we
                            # discard rather than popping.
                            available_macs.discard(port['mac_address'])
                    network_id = port['network_id']
                    ports[network_id] = port
                elif fixed_ip and network_id:
                    fixed_ips[network_id] = fixed_ip
                if network_id:
                    net_ids.append(network_id)

        nets = self._get_available_networks(context, instance['project_id'],
                                            net_ids)
        security_groups = kwargs.get('security_groups', [])
        security_group_ids = []

        # TODO(arosen) Should optimize more to do direct query for security
        # group if len(security_groups) == 1
        if len(security_groups):
            search_opts = {'tenant_id': instance['project_id']}
            user_security_groups = quantum.list_security_groups(
                **search_opts).get('security_groups')

        for security_group in security_groups:
            name_match = None
            uuid_match = None
            for user_security_group in user_security_groups:
                if user_security_group['name'] == security_group:
                    if name_match:
                        msg = (_("Multiple security groups found matching"
                                 " '%s'. Use an ID to be more specific."),
                                 security_group)
                        raise exception.NoUniqueMatch(msg)
                    name_match = user_security_group['id']
                if user_security_group['id'] == security_group:
                    uuid_match = user_security_group['id']

            # If a user names the security group the same as
            # another's security groups uuid, the name takes priority.
            if not name_match and not uuid_match:
                raise exception.SecurityGroupNotFound(
                    security_group_id=security_group)
                security_group_ids.append(name_match)
            elif name_match:
                security_group_ids.append(name_match)
            elif uuid_match:
                security_group_ids.append(uuid_match)

        touched_port_ids = []
        created_port_ids = []
        for network in nets:
            # If security groups are requested on an instance then the
            # network must has a subnet associated with it. Some plugins
            # implement the port-security extension which requires
            # 'port_security_enabled' to be True for security groups.
            # That is why True is returned if 'port_security_enabled'
            # is not found.
            if (security_groups and not (
                    network['subnets']
                    and network.get('port_security_enabled', True))):

                raise exception.SecurityGroupCannotBeApplied()
            network_id = network['id']
            zone = 'compute:%s' % instance['availability_zone']
            port_req_body = {'port': {'device_id': instance['uuid'],
                                      'device_owner': zone}}
            try:
                port = ports.get(network_id)
                self._populate_quantum_extension_values(instance,
                                                        port_req_body)
                # Requires admin creds to set port bindings
                port_client = (quantum if not
                               self._has_port_binding_extension() else
                               quantumv2.get_client(context, admin=True))
                if port:
                    port_client.update_port(port['id'], port_req_body)
                    touched_port_ids.append(port['id'])
                else:
                    fixed_ip = fixed_ips.get(network_id)
                    if fixed_ip:
                        port_req_body['port']['fixed_ips'] = [{'ip_address':
                                                               fixed_ip}]
                    port_req_body['port']['network_id'] = network_id
                    port_req_body['port']['admin_state_up'] = True
                    port_req_body['port']['tenant_id'] = instance['project_id']
                    if security_group_ids:
                        port_req_body['port']['security_groups'] = (
                            security_group_ids)
                    if available_macs is not None:
                        if not available_macs:
                            raise exception.PortNotFree(
                                instance=instance['display_name'])
                        mac_address = available_macs.pop()
                        port_req_body['port']['mac_address'] = mac_address
                    created_port_ids.append(
                        port_client.create_port(port_req_body)['port']['id'])
            except Exception:
                with excutils.save_and_reraise_exception():
                    for port_id in touched_port_ids:
                        try:
                            port_req_body = {'port': {'device_id': None}}
                            # Requires admin creds to set port bindings
                            if self._has_port_binding_extension():
                                port_req_body['port']['binding:host_id'] = None
                                port_client = quantumv2.get_client(
                                    context, admin=True)
                            else:
                                port_client = quantum
                            port_client.update_port(port_id, port_req_body)
                        except Exception:
                            msg = _("Failed to update port %s")
                            LOG.exception(msg, port_id)

                    for port_id in created_port_ids:
                        try:
                            quantum.delete_port(port_id)
                        except Exception:
                            msg = _("Failed to delete port %s")
                            LOG.exception(msg, port_id)

        self.trigger_security_group_members_refresh(context, instance)
        self.trigger_instance_add_security_group_refresh(context, instance)

        nw_info = self.get_instance_nw_info(context, instance,
                conductor_api=kwargs.get('conductor_api'), networks=nets)
        # NOTE(danms): Only return info about ports we created in this run.
        # In the initial allocation case, this will be everything we created,
        # and in later runs will only be what was created that time. Thus,
        # this only affects the attach case, not the original use for this
        # method.
        return network_model.NetworkInfo([port for port in nw_info
                                          if port['id'] in created_port_ids +
                                                           touched_port_ids])

    def _refresh_quantum_extensions_cache(self):
        if (not self.last_quantum_extension_sync or
            ((time.time() - self.last_quantum_extension_sync)
             >= CONF.quantum_extension_sync_interval)):
            quantum = quantumv2.get_client(context.get_admin_context())
            extensions_list = quantum.list_extensions()['extensions']
            self.last_quantum_extension_sync = time.time()
            self.extensions.clear()
            self.extensions = dict((ext['name'], ext)
                                   for ext in extensions_list)

    def _has_port_binding_extension(self, refresh_cache=False):
        if refresh_cache:
            self._refresh_quantum_extensions_cache()
        return constants.PORTBINDING_EXT in self.extensions

    def _populate_quantum_extension_values(self, instance, port_req_body):
        self._refresh_quantum_extensions_cache()
        if 'nvp-qos' in self.extensions:
            instance_type = instance_types.extract_instance_type(instance)
            rxtx_factor = instance_type.get('rxtx_factor')
            port_req_body['port']['rxtx_factor'] = rxtx_factor
        if self._has_port_binding_extension():
            port_req_body['port']['binding:host_id'] = instance.get('host')

    def deallocate_for_instance(self, context, instance, **kwargs):
        """Deallocate all network resources related to the instance."""
        LOG.debug(_('deallocate_for_instance() for %s'),
                  instance['display_name'])
        search_opts = {'device_id': instance['uuid']}
        data = quantumv2.get_client(context).list_ports(**search_opts)
        ports = data.get('ports', [])
        for port in ports:
            try:
                quantumv2.get_client(context).delete_port(port['id'])
            except Exception:
                LOG.exception(_("Failed to delete quantum port %(portid)s")
                              % {'portid': port['id']})
        self.trigger_security_group_members_refresh(context, instance)
        self.trigger_instance_remove_security_group_refresh(context, instance)

    def allocate_port_for_instance(self, context, instance, port_id,
                                   network_id=None, requested_ip=None,
                                   conductor_api=None):
        return self.allocate_for_instance(context, instance,
                requested_networks=[(network_id, requested_ip, port_id)],
                conductor_api=conductor_api)

    def deallocate_port_for_instance(self, context, instance, port_id,
                                     conductor_api=None):
        """Remove a specified port from the instance.

        Return network information for the instance
        """
        try:
            quantumv2.get_client(context).delete_port(port_id)
        except Exception as ex:
            LOG.exception(_("Failed to delete quantum port %(port_id)s ") %
                          locals())

        self.trigger_security_group_members_refresh(context, instance)
        self.trigger_instance_remove_security_group_refresh(context, instance)

        return self.get_instance_nw_info(context, instance, 
                                                conductor_api=conductor_api)

    def list_ports(self, context, **search_opts):
        return quantumv2.get_client(context).list_ports(**search_opts)

    def show_port(self, context, port_id):
        return quantumv2.get_client(context).show_port(port_id)

    @refresh_cache
    def get_instance_nw_info(self, context, instance, conductor_api=None,
                             networks=None):
        """Return network information for specified instance
           and update cache.
        """
        result = self._get_instance_nw_info(context, instance, networks)
        return result

    def _get_instance_nw_info(self, context, instance, networks=None):
        # keep this caching-free version of the get_instance_nw_info method
        # because it is used by the caching logic itself.
        LOG.debug(_('get_instance_nw_info() for %s'), instance['display_name'])
        nw_info = self._build_network_info_model(context, instance, networks)
        return network_model.NetworkInfo.hydrate(nw_info)

    @refresh_cache
    def add_fixed_ip_to_instance(self, context, instance, network_id,
                                 conductor_api=None):
        """Add a fixed ip to the instance from specified network."""
        search_opts = {'network_id': network_id}
        data = quantumv2.get_client(context).list_subnets(**search_opts)
        ipam_subnets = data.get('subnets', [])
        if not ipam_subnets:
            raise exception.NetworkNotFoundForInstance(
                instance_id=instance['uuid'])

        zone = 'compute:%s' % instance['availability_zone']
        search_opts = {'device_id': instance['uuid'],
                       'device_owner': zone,
                       'network_id': network_id}
        data = quantumv2.get_client(context).list_ports(**search_opts)
        ports = data['ports']
        for p in ports:
            for subnet in ipam_subnets:
                fixed_ips = p['fixed_ips']
                fixed_ips.append({'subnet_id': subnet['id']})
                port_req_body = {'port': {'fixed_ips': fixed_ips}}
                try:
                    quantumv2.get_client(context).update_port(p['id'],
                                                              port_req_body)
                    return
                except Exception as ex:
                    msg = _("Unable to update port %(portid)s on subnet "
                            "%(subnet_id)s with failure: %(exception)s")
                    LOG.debug(msg, {'portid': p['id'],
                                    'subnet_id': subnet['id'],
                                    'exception': ex})

        raise exception.NetworkNotFoundForInstance(
                instance_id=instance['uuid'])

    @refresh_cache
    def remove_fixed_ip_from_instance(self, context, instance, address,
                                      conductor_api=None):
        """Remove a fixed ip from the instance."""
        zone = 'compute:%s' % instance['availability_zone']
        search_opts = {'device_id': instance['uuid'],
                       'device_owner': zone,
                       'fixed_ips': 'ip_address=%s' % address}
        data = quantumv2.get_client(context).list_ports(**search_opts)
        ports = data['ports']
        for p in ports:
            fixed_ips = p['fixed_ips']
            new_fixed_ips = []
            for fixed_ip in fixed_ips:
                if fixed_ip['ip_address'] != address:
                    new_fixed_ips.append(fixed_ip)
            port_req_body = {'port': {'fixed_ips': new_fixed_ips}}
            try:
                quantumv2.get_client(context).update_port(p['id'],
                                                          port_req_body)
            except Exception as ex:
                msg = _("Unable to update port %(portid)s with"
                        " failure: %(exception)s")
                LOG.debug(msg, {'portid': p['id'], 'exception': ex})
            return

        raise exception.FixedIpNotFoundForSpecificInstance(
                instance_uuid=instance['uuid'], ip=address)

    def validate_networks(self, context, requested_networks):
        """Validate that the tenant can use the requested networks."""
        LOG.debug(_('validate_networks() for %s'),
                  requested_networks)
        if not requested_networks:
            return
        net_ids = []

        for (net_id, _i, port_id) in requested_networks:
            if port_id:
                port = (quantumv2.get_client(context)
                                 .show_port(port_id)
                                 .get('port'))
                if not port:
                    raise exception.PortNotFound(port_id=port_id)
                if port.get('device_id', None):
                    raise exception.PortInUse(port_id=port_id)
                net_id = port['network_id']
            if net_id in net_ids:
                raise exception.NetworkDuplicated(network_id=net_id)
            net_ids.append(net_id)

        nets = self._get_available_networks(context, context.project_id,
                                            net_ids)
        if len(nets) != len(net_ids):
            requsted_netid_set = set(net_ids)
            returned_netid_set = set([net['id'] for net in nets])
            lostid_set = requsted_netid_set - returned_netid_set
            id_str = ''
            for _id in lostid_set:
                id_str = id_str and id_str + ', ' + _id or _id
            raise exception.NetworkNotFound(network_id=id_str)

    def _get_instance_uuids_by_ip(self, context, address):
        """Retrieve instance uuids associated with the given ip address.

        :returns: A list of dicts containing the uuids keyed by 'instance_uuid'
                  e.g. [{'instance_uuid': uuid}, ...]
        """
        search_opts = {"fixed_ips": 'ip_address=%s' % address}
        data = quantumv2.get_client(context).list_ports(**search_opts)
        ports = data.get('ports', [])
        return [{'instance_uuid': port['device_id']} for port in ports
                if port['device_id']]

    def get_instance_uuids_by_ip_filter(self, context, filters):
        """Return a list of dicts in the form of
        [{'instance_uuid': uuid}] that matched the ip filter.
        """
        # filters['ip'] is composed as '^%s$' % fixed_ip.replace('.', '\\.')
        ip = filters.get('ip')
        # we remove ^$\ in the ip filer
        if ip[0] == '^':
            ip = ip[1:]
        if ip[-1] == '$':
            ip = ip[:-1]
        ip = ip.replace('\\.', '.')
        return self._get_instance_uuids_by_ip(context, ip)

    def trigger_instance_add_security_group_refresh(self, context,
                                                    instance_ref):
        admin_context = context.elevated()
        for group in instance_ref['security_groups']:
            self.conductor_api.security_groups_trigger_handler(context,
                'instance_add_security_group', instance_ref, group['name'])

    def trigger_instance_remove_security_group_refresh(self, context,
                                                       instance_ref):
        admin_context = context.elevated()
        for group in instance_ref['security_groups']:
            self.conductor_api.security_groups_trigger_handler(context,
                'instance_remove_security_group', instance_ref, group['name'])

    def trigger_security_group_members_refresh(self, context, instance_ref):

        admin_context = context.elevated()
        group_ids = [group['id'] for group in instance_ref['security_groups']]

        self.conductor_api.security_groups_trigger_members_refresh(
            admin_context, group_ids)
        self.conductor_api.security_groups_trigger_handler(admin_context,
            'security_group_members', group_ids)

    def _get_port_id_by_fixed_address(self, client,
                                      instance, address):
        zone = 'compute:%s' % instance['availability_zone']
        search_opts = {'device_id': instance['uuid'],
                       'device_owner': zone}
        data = client.list_ports(**search_opts)
        ports = data['ports']
        port_id = None
        for p in ports:
            for ip in p['fixed_ips']:
                if ip['ip_address'] == address:
                    port_id = p['id']
                    break
        if not port_id:
            raise exception.FixedIpNotFoundForAddress(address=address)
        return port_id

    @refresh_cache
    def associate_floating_ip(self, context, instance,
                              floating_address, fixed_address,
                              affect_auto_assigned=False):
        """Associate a floating ip with a fixed ip."""

        # Note(amotoki): 'affect_auto_assigned' is not respected
        # since it is not used anywhere in nova code and I could
        # find why this parameter exists.

        client = quantumv2.get_client(context)
        port_id = self._get_port_id_by_fixed_address(client, instance,
                                                     fixed_address)
        fip = self._get_floating_ip_by_address(client, floating_address)
        param = {'port_id': port_id,
                 'fixed_ip_address': fixed_address}
        client.update_floatingip(fip['id'], {'floatingip': param})

    def get_all(self, context):
        client = quantumv2.get_client(context)
        networks = client.list_networks().get('networks') or {}
        for network in networks:
            network['label'] = network['name']
        return networks

    def get(self, context, network_uuid):
        client = quantumv2.get_client(context)
        network = client.show_network(network_uuid).get('network') or {}
        network['label'] = network['name']
        return network

    def delete(self, context, network_uuid):
        raise NotImplementedError()

    def disassociate(self, context, network_uuid):
        raise NotImplementedError()

    def get_fixed_ip(self, context, id):
        raise NotImplementedError()

    def get_fixed_ip_by_address(self, context, address):
        uuid_maps = self._get_instance_uuids_by_ip(context, address)
        if len(uuid_maps) == 1:
            return uuid_maps[0]
        elif not uuid_maps:
            raise exception.FixedIpNotFoundForAddress(address=address)
        else:
            raise exception.FixedIpAssociatedWithMultipleInstances(
                address=address)

    def _setup_net_dict(self, client, network_id):
        if not network_id:
            return {}
        pool = client.show_network(network_id)['network']
        return {pool['id']: pool}

    def _setup_port_dict(self, client, port_id):
        if not port_id:
            return {}
        port = client.show_port(port_id)['port']
        return {port['id']: port}

    def _setup_pools_dict(self, client):
        pools = self._get_floating_ip_pools(client)
        return dict([(i['id'], i) for i in pools])

    def _setup_ports_dict(self, client, project_id=None):
        search_opts = {'tenant_id': project_id} if project_id else {}
        ports = client.list_ports(**search_opts)['ports']
        return dict([(p['id'], p) for p in ports])

    def get_floating_ip(self, context, id):
        client = quantumv2.get_client(context)
        fip = client.show_floatingip(id)['floatingip']
        pool_dict = self._setup_net_dict(client,
                                         fip['floating_network_id'])
        port_dict = self._setup_port_dict(client, fip['port_id'])
        return self._format_floating_ip_model(fip, pool_dict, port_dict)

    def _get_floating_ip_pools(self, client, project_id=None):
        search_opts = {constants.NET_EXTERNAL: True}
        if project_id:
            search_opts.update({'tenant_id': project_id})
        data = client.list_networks(**search_opts)
        return data['networks']

    def get_floating_ip_pools(self, context):
        client = quantumv2.get_client(context)
        pools = self._get_floating_ip_pools(client)
        return [{'name': n['name'] or n['id']} for n in pools]

    def _format_floating_ip_model(self, fip, pool_dict, port_dict):
        pool = pool_dict[fip['floating_network_id']]
        result = {'id': fip['id'],
                  'address': fip['floating_ip_address'],
                  'pool': pool['name'] or pool['id'],
                  'project_id': fip['tenant_id'],
                  # In Quantum v2, an exact fixed_ip_id does not exist.
                  'fixed_ip_id': fip['port_id'],
                  }
        # In Quantum v2 API fixed_ip_address and instance uuid
        # (= device_id) are known here, so pass it as a result.
        result['fixed_ip'] = {'address': fip['fixed_ip_address']}
        if fip['port_id']:
            instance_uuid = port_dict[fip['port_id']]['device_id']
            result['instance'] = {'uuid': instance_uuid}
        else:
            result['instance'] = None
        return result

    def get_floating_ip_by_address(self, context, address):
        client = quantumv2.get_client(context)
        fip = self._get_floating_ip_by_address(client, address)
        pool_dict = self._setup_net_dict(client,
                                         fip['floating_network_id'])
        port_dict = self._setup_port_dict(client, fip['port_id'])
        return self._format_floating_ip_model(fip, pool_dict, port_dict)

    def get_floating_ips_by_project(self, context):
        client = quantumv2.get_client(context)
        project_id = context.project_id
        fips = client.list_floatingips(tenant_id=project_id)['floatingips']
        pool_dict = self._setup_pools_dict(client)
        port_dict = self._setup_ports_dict(client, project_id)
        return [self._format_floating_ip_model(fip, pool_dict, port_dict)
                for fip in fips]

    def get_floating_ips_by_fixed_address(self, context, fixed_address):
        return []

    def get_instance_id_by_floating_address(self, context, address):
        """Returns the instance id a floating ip's fixed ip is allocated to."""
        client = quantumv2.get_client(context)
        fip = self._get_floating_ip_by_address(client, address)
        if not fip['port_id']:
            return None
        port = client.show_port(fip['port_id'])['port']
        return port['device_id']

    def get_vifs_by_instance(self, context, instance):
        raise NotImplementedError()

    def get_vif_by_mac_address(self, context, mac_address):
        raise NotImplementedError()

    def _get_floating_ip_pool_id_by_name_or_id(self, client, name_or_id):
        search_opts = {constants.NET_EXTERNAL: True, 'fields': 'id'}
        if uuidutils.is_uuid_like(name_or_id):
            search_opts.update({'id': name_or_id})
        else:
            search_opts.update({'name': name_or_id})
        data = client.list_networks(**search_opts)
        nets = data['networks']

        if len(nets) == 1:
            return nets[0]['id']
        elif len(nets) == 0:
            raise exception.FloatingIpPoolNotFound()
        else:
            msg = (_("Multiple floating IP pools matches found for name '%s'")
                   % name_or_id)
            raise exception.NovaException(message=msg)

    def allocate_floating_ip(self, context, pool=None):
        """Add a floating ip to a project from a pool."""
        client = quantumv2.get_client(context)
        pool = pool or CONF.default_floating_pool
        pool_id = self._get_floating_ip_pool_id_by_name_or_id(client, pool)

        # TODO(amotoki): handle exception during create_floatingip()
        # At this timing it is ensured that a network for pool exists.
        # quota error may be returned.
        param = {'floatingip': {'floating_network_id': pool_id}}
        fip = client.create_floatingip(param)
        return fip['floatingip']['floating_ip_address']

    def _get_floating_ip_by_address(self, client, address):
        """Get floatingip from floating ip address."""
        data = client.list_floatingips(floating_ip_address=address)
        fips = data['floatingips']
        if len(fips) == 0:
            raise exception.FloatingIpNotFoundForAddress(address=address)
        elif len(fips) > 1:
            raise exception.FloatingIpMultipleFoundForAddress(address=address)
        return fips[0]

    def _get_floating_ips_by_fixed_and_port(self, client, fixed_ip, port):
        """Get floatingips from fixed ip and port."""
        try:
            data = client.list_floatingips(fixed_ip_address=fixed_ip,
                                           port_id=port)
        # If a quantum plugin does not implement the L3 API a 404 from
        # list_floatingips will be raised.
        except qexceptions.QuantumClientException as e:
            if e.status_code == 404:
                return []
            raise
        return data['floatingips']

    def release_floating_ip(self, context, address,
                            affect_auto_assigned=False):
        """Remove a floating ip with the given address from a project."""

        # Note(amotoki): We cannot handle a case where multiple pools
        # have overlapping IP address range. In this case we cannot use
        # 'address' as a unique key.
        # This is a limitation of the current nova.

        # Note(amotoki): 'affect_auto_assigned' is not respected
        # since it is not used anywhere in nova code and I could
        # find why this parameter exists.

        client = quantumv2.get_client(context)
        fip = self._get_floating_ip_by_address(client, address)
        if fip['port_id']:
            raise exception.FloatingIpAssociated(address=address)
        client.delete_floatingip(fip['id'])

    @refresh_cache
    def disassociate_floating_ip(self, context, instance, address,
                                 affect_auto_assigned=False):
        """Disassociate a floating ip from the instance."""

        # Note(amotoki): 'affect_auto_assigned' is not respected
        # since it is not used anywhere in nova code and I could
        # find why this parameter exists.

        client = quantumv2.get_client(context)
        fip = self._get_floating_ip_by_address(client, address)
        client.update_floatingip(fip['id'], {'floatingip': {'port_id': None}})

    def migrate_instance_start(self, context, instance, migration):
        """Start to migrate the network of an instance."""
        # NOTE(wenjianhn): just pass to make migrate instance doesn't
        # raise for now.
        pass

    def migrate_instance_finish(self, context, instance, migration):
        """Finish migrating the network of an instance."""
        LOG.info("vagration is finishing.....")
        if not self._has_port_binding_extension(refresh_cache=True):
            return

        quantum = quantumv2.get_client(context, admin=True)
        network_info = self.get_instance_nw_info(context,instance,self.conductor_api)
        search_opts = {'device_id': instance['uuid'],
                       'tenant_id': instance['project_id']}
        data = quantum.list_ports(**search_opts)
        LOG.info("port update at %s", instance.get('host'))
        ports = data['ports']
        for p in ports:
            port_req_body = {'port': {'binding:host_id': instance.get('host')}}
            try:
                quantum.update_port(p['id'], port_req_body)
            except Exception as ex:
                with excutils.save_and_reraise_exception():
                    msg = _("Unable to update host of port %s")
                    LOG.exception(msg, p['id'])


    def add_network_to_project(self, context, project_id, network_uuid=None):
        """Force add a network to the project."""
        raise NotImplementedError()

    def _build_network_info_model(self, context, instance, networks=None):
        # Note(arosen): on interface-attach networks only contains the
        # network that the interface is being attached to.

        search_opts = {'tenant_id': instance['project_id'],
                       'device_id': instance['uuid'], }
        client = quantumv2.get_client(context, admin=True)
        data = client.list_ports(**search_opts)
        ports = data.get('ports', [])
        nw_info = network_model.NetworkInfo()
        if networks is None:
            # retrieve networks from info_cache to get correct nic order
            network_cache = self.conductor_api.instance_get_by_uuid(
                context, instance['uuid'])['info_cache']['network_info']
            network_cache = jsonutils.loads(network_cache)
            net_ids = [iface['network']['id'] for iface in network_cache]
            networks = self._get_available_networks(context,
                                                    instance['project_id'])

        # ensure ports are in preferred network order, and filter out
        # those not attached to one of the provided list of networks
        else:

            # Unfortunately, this is sometimes in unicode and sometimes not
            if isinstance(instance['info_cache']['network_info'], unicode):
                ifaces = jsonutils.loads(
                    instance['info_cache']['network_info'])
            else:
                ifaces = instance['info_cache']['network_info']

            # Include existing interfaces so they are not removed from the db.
            # Needed when interfaces are added to existing instances.
            for iface in ifaces:
                nw_info.append(network_model.VIF(
                    id=iface['id'],
                    address=iface['address'],
                    network=iface['network'],
                    type=iface['type'],
                    ovs_interfaceid=iface['ovs_interfaceid'],
                    devname=iface['devname']))

            net_ids = [n['id'] for n in networks]

        ports = [port for port in ports if port['network_id'] in net_ids]
        _ensure_requested_network_ordering(lambda x: x['network_id'],
                                           ports, net_ids)

        for port in ports:
            # NOTE(danms): This loop can't fail to find a network since we
            # filtered ports to only the ones matching networks above.
            for net in networks:
                if port['network_id'] == net['id']:
                    network_name = net['name']
                    break

            network_IPs = []
            for fixed_ip in port['fixed_ips']:
                fixed = network_model.FixedIP(address=fixed_ip['ip_address'])
                floats = self._get_floating_ips_by_fixed_and_port(
                        client, fixed_ip['ip_address'], port['id'])
                for ip in floats:
                    fip = network_model.IP(address=ip['floating_ip_address'],
                                           type='floating')
                    fixed.add_floating_ip(fip)
                network_IPs.append(fixed)

            subnets = self._get_subnets_from_port(context, port)
            for subnet in subnets:
                subnet['ips'] = [fixed_ip for fixed_ip in network_IPs
                                 if fixed_ip.is_in_subnet(subnet)]

            bridge = None
            ovs_interfaceid = None
            # Network model metadata
            should_create_bridge = None
            vif_type = port.get('binding:vif_type')
            # TODO(berrange) Quantum should pass the bridge name
            # in another binding metadata field
            if vif_type == network_model.VIF_TYPE_OVS:
                bridge = CONF.quantum_ovs_bridge
                ovs_interfaceid = port['id']
            elif vif_type == network_model.VIF_TYPE_BRIDGE:
                bridge = "brq" + port['network_id']
                should_create_bridge = True

            if bridge is not None:
                bridge = bridge[:network_model.NIC_NAME_LEN]

            devname = "tap" + port['id']
            devname = devname[:network_model.NIC_NAME_LEN]

            network = network_model.Network(
                id=port['network_id'],
                bridge=bridge,
                injected=CONF.flat_injected,
                label=network_name,
                tenant_id=net['tenant_id']
            )
            network['subnets'] = subnets
            if should_create_bridge is not None:
                network['should_create_bridge'] = should_create_bridge
            nw_info.append(network_model.VIF(
                id=port['id'],
                address=port['mac_address'],
                network=network,
                type=port.get('binding:vif_type'),
                ovs_interfaceid=ovs_interfaceid,
                devname=devname))
        return nw_info

    def _get_subnets_from_port(self, context, port):
        """Return the subnets for a given port."""

        fixed_ips = port['fixed_ips']
        # No fixed_ips for the port means there is no subnet associated
        # with the network the port is created on.
        # Since list_subnets(id=[]) returns all subnets visible for the
        # current tenant, returned subnets may contain subnets which is not
        # related to the port. To avoid this, the method returns here.
        if not fixed_ips:
            return []
        search_opts = {'id': [ip['subnet_id'] for ip in fixed_ips]}
        data = quantumv2.get_client(context).list_subnets(**search_opts)
        ipam_subnets = data.get('subnets', [])
        subnets = []

        for subnet in ipam_subnets:
            subnet_dict = {'cidr': subnet['cidr'],
                           'gateway': network_model.IP(
                                address=subnet['gateway_ip'],
                                type='gateway'),
            }

            # attempt to populate DHCP server field
            search_opts = {'network_id': subnet['network_id'],
                           'device_owner': 'network:dhcp'}
            data = quantumv2.get_client(context).list_ports(**search_opts)
            dhcp_ports = data.get('ports', [])
            for p in dhcp_ports:
                for ip_pair in p['fixed_ips']:
                    if ip_pair['subnet_id'] == subnet['id']:
                        subnet_dict['dhcp_server'] = ip_pair['ip_address']
                        break

            subnet_object = network_model.Subnet(**subnet_dict)
            for dns in subnet.get('dns_nameservers', []):
                subnet_object.add_dns(
                    network_model.IP(address=dns, type='dns'))

            # TODO(gongysh) get the routes for this subnet
            subnets.append(subnet_object)
        return subnets

    def get_dns_domains(self, context):
        """Return a list of available dns domains.

        These can be used to create DNS entries for floating ips.
        """
        raise NotImplementedError()

    def get_network_id_by_name(self, context, network_name=None):
        quantum = quantumv2.get_client(context)
        try:
          network_id = quantumv20.find_resourceid_by_name_or_id(
                  quantum, 'network', network_name)
        except Exception:
                with excutils.save_and_reraise_exception():
                    LOG.exception(_("Unable to get ID of network '%s'")
                                   %network_name)
        return network_id

    def add_dns_entry(self, context, address, name, dns_type, domain):
        """Create specified DNS entry for address."""
        raise NotImplementedError()

    def modify_dns_entry(self, context, name, address, domain):
        """Create specified DNS entry for address."""
        raise NotImplementedError()

    def delete_dns_entry(self, context, name, domain):
        """Delete the specified dns entry."""
        raise NotImplementedError()

    def delete_dns_domain(self, context, domain):
        """Delete the specified dns domain."""
        raise NotImplementedError()

    def get_dns_entries_by_address(self, context, address, domain):
        """Get entries for address and domain."""
        raise NotImplementedError()

    def get_dns_entries_by_name(self, context, name, domain):
        """Get entries for name and domain."""
        raise NotImplementedError()

    def create_private_dns_domain(self, context, domain, availability_zone):
        """Create a private DNS domain with nova availability zone."""
        raise NotImplementedError()

    def create_public_dns_domain(self, context, domain, project=None):
        """Create a private DNS domain with optional nova project."""
        raise NotImplementedError()

    def _format_agents_status(self, context, agent):
        result = {'agent_type': agent['agent_type'],
                  'alive': agent['alive']}
        return result
    
    def get_agents_status(self, context):
        agents = quantumv2.get_client(context).list_agents()
        agents = agents['agents']

        result = {}
        for agent in agents:
            if not result.get(agent['host']):
                result[agent['host']] = []
            status = self._format_agents_status(context, agent)
            result[agent['host']].append(status)
        return result

def _ensure_requested_network_ordering(accessor, unordered, preferred):
    """Sort a list with respect to the preferred network ordering."""
    if preferred:
        unordered.sort(key=lambda i: preferred.index(accessor(i)))
