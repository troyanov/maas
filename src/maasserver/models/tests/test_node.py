# Copyright 2012-2014 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test maasserver models."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = []

from datetime import timedelta
from itertools import izip
import random

import celery
from django.core.exceptions import ValidationError
from maasserver.clusterrpc.power_parameters import get_power_types
from maasserver.dns import config as dns_config
from maasserver.enum import (
    IPADDRESS_TYPE,
    NODE_BOOT,
    NODE_PERMISSION,
    NODE_STATUS,
    NODE_STATUS_CHOICES,
    NODE_STATUS_CHOICES_DICT,
    NODEGROUP_STATUS,
    NODEGROUPINTERFACE_MANAGEMENT,
    POWER_STATE,
    )
from maasserver.exceptions import (
    NodeStateViolation,
    StaticIPAddressExhaustion,
    StaticIPAddressTypeClash,
    )
from maasserver.fields import MAC
from maasserver.models import (
    Config,
    LicenseKey,
    MACAddress,
    Node,
    node as node_module,
    )
from maasserver.models.node import (
    NODE_TRANSITIONS,
    validate_hostname,
    )
from maasserver.models.staticipaddress import (
    StaticIPAddress,
    StaticIPAddressManager,
    )
from maasserver.models.user import create_auth_token
from maasserver.rpc.testing.fixtures import MockLiveRegionToClusterRPCFixture
from maasserver.testing.eventloop import (
    RegionEventLoopFixture,
    RunningEventLoopFixture,
    )
from maasserver.testing.factory import factory
from maasserver.testing.orm import reload_object
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils import ignore_unused
from maastesting.djangotestcase import count_queries
from maastesting.matchers import (
    MockCalledOnceWith,
    MockNotCalled,
    )
from maastesting.testcase import MAASTestCase
from metadataserver import commissioning
from metadataserver.enum import RESULT_TYPE
from metadataserver.fields import Bin
from metadataserver.models import (
    NodeResult,
    NodeUserData,
    )
from mock import (
    ANY,
    sentinel,
    )
from provisioningserver.power.poweraction import PowerAction
from provisioningserver.rpc import cluster
from provisioningserver.rpc.exceptions import MultipleFailures
from provisioningserver.rpc.testing import (
    always_succeed_with,
    TwistedLoggerFixture,
    )
from provisioningserver.tasks import Omshell
from provisioningserver.utils.enum import map_enum
from testtools.matchers import (
    AfterPreprocessing,
    Equals,
    HasLength,
    MatchesStructure,
    )
from twisted.internet import defer
from twisted.python.failure import Failure


class TestHostnameValidator(MAASTestCase):
    """Tests for the validation of hostnames.

    Specifications based on:
        http://en.wikipedia.org/wiki/Hostname#Restrictions_on_valid_host_names

    This does not support Internationalized Domain Names.  To do so, we'd have
    to accept and store unicode, but use the Punycode-encoded version.  The
    validator would have to validate both versions: the unicode input for
    invalid characters, and the encoded version for length.
    """
    def make_maximum_hostname(self):
        """Create a hostname of the maximum permitted length.

        The maximum permitted length is 255 characters.  The last label in the
        hostname will not be of the maximum length, so tests can still append a
        character to it without creating an invalid label.

        The hostname is not randomised, so do not count on it being unique.
        """
        # A hostname may contain any number of labels, separated by dots.
        # Each of the labels has a maximum length of 63 characters, so this has
        # to be built up from multiple labels.
        ten_chars = ('a' * 9) + '.'
        hostname = ten_chars * 25 + ('b' * 5)
        self.assertEqual(255, len(hostname))
        return hostname

    def assertAccepts(self, hostname):
        """Assertion: the validator accepts `hostname`."""
        try:
            validate_hostname(hostname)
        except ValidationError as e:
            raise AssertionError(unicode(e))

    def assertRejects(self, hostname):
        """Assertion: the validator rejects `hostname`."""
        self.assertRaises(ValidationError, validate_hostname, hostname)

    def test_accepts_ascii_letters(self):
        self.assertAccepts('abcde')

    def test_accepts_dots(self):
        self.assertAccepts('abc.def')

    def test_rejects_adjacent_dots(self):
        self.assertRejects('abc..def')

    def test_rejects_leading_dot(self):
        self.assertRejects('.abc')

    def test_rejects_trailing_dot(self):
        self.assertRejects('abc.')

    def test_accepts_ascii_digits(self):
        self.assertAccepts('abc123')

    def test_accepts_leading_digits(self):
        # Leading digits used to be forbidden, but are now allowed.
        self.assertAccepts('123abc')

    def test_rejects_whitespace(self):
        self.assertRejects('a b')
        self.assertRejects('a\nb')
        self.assertRejects('a\tb')

    def test_rejects_other_ascii_characters(self):
        self.assertRejects('a?b')
        self.assertRejects('a!b')
        self.assertRejects('a,b')
        self.assertRejects('a:b')
        self.assertRejects('a;b')
        self.assertRejects('a+b')
        self.assertRejects('a=b')

    def test_accepts_underscore_in_domain(self):
        self.assertAccepts('host.local_domain')

    def test_rejects_underscore_in_host(self):
        self.assertRejects('host_name.local')

    def test_accepts_hyphen(self):
        self.assertAccepts('a-b')

    def test_rejects_hyphen_at_start_of_label(self):
        self.assertRejects('-ab')

    def test_rejects_hyphen_at_end_of_label(self):
        self.assertRejects('ab-')

    def test_accepts_maximum_valid_length(self):
        self.assertAccepts(self.make_maximum_hostname())

    def test_rejects_oversized_hostname(self):
        self.assertRejects(self.make_maximum_hostname() + 'x')

    def test_accepts_maximum_label_length(self):
        self.assertAccepts('a' * 63)

    def test_rejects_oversized_label(self):
        self.assertRejects('b' * 64)

    def test_rejects_nonascii_letter(self):
        # The \u03be is the Greek letter xi.  Perfectly good letter, just not
        # ASCII.
        self.assertRejects('\u03be')


def make_active_lease(nodegroup=None):
    """Create a `DHCPLease` on a managed `NodeGroupInterface`."""
    lease = factory.make_dhcp_lease(nodegroup=nodegroup)
    factory.make_node_group_interface(
        lease.nodegroup, management=NODEGROUPINTERFACE_MANAGEMENT.DHCP)
    return lease


class NodeTest(MAASServerTestCase):

    def test_system_id(self):
        """
        The generated system_id looks good.

        """
        node = factory.make_node()
        self.assertEqual(len(node.system_id), 41)
        self.assertTrue(node.system_id.startswith('node-'))

    def test_hostname_is_validated(self):
        bad_hostname = '-_?!@*-'
        self.assertRaises(
            ValidationError,
            factory.make_node, hostname=bad_hostname)

    def test_work_queue_returns_nodegroup_uuid(self):
        nodegroup = factory.make_node_group()
        node = factory.make_node(nodegroup=nodegroup)
        self.assertEqual(nodegroup.uuid, node.work_queue)

    def test_display_status_shows_default_status(self):
        node = factory.make_node()
        self.assertEqual(
            NODE_STATUS_CHOICES_DICT[node.status],
            node.display_status())

    def test_display_memory_returns_decimal_less_than_1024(self):
        node = factory.make_node(memory=512)
        self.assertEqual('0.5', node.display_memory())

    def test_display_memory_returns_value_divided_by_1024(self):
        node = factory.make_node(memory=2048)
        self.assertEqual('2', node.display_memory())

    def test_display_storage_returns_decimal_less_than_1024(self):
        node = factory.make_node(storage=512)
        self.assertEqual('0.5', node.display_storage())

    def test_display_storage_returns_value_divided_by_1024(self):
        node = factory.make_node(storage=2048)
        self.assertEqual('2', node.display_storage())

    def test_add_node_with_token(self):
        user = factory.make_user()
        token = create_auth_token(user)
        node = factory.make_node(token=token)
        self.assertEqual(token, node.token)

    def test_add_mac_address(self):
        mac = factory.getRandomMACAddress()
        node = factory.make_node()
        node.add_mac_address(mac)
        macs = MACAddress.objects.filter(node=node, mac_address=mac).count()
        self.assertEqual(1, macs)

    def test_remove_mac_address(self):
        mac = factory.getRandomMACAddress()
        node = factory.make_node()
        node.add_mac_address(mac)
        node.remove_mac_address(mac)
        self.assertItemsEqual(
            [],
            MACAddress.objects.filter(node=node, mac_address=mac))

    def test_get_primary_mac_returns_mac_address(self):
        node = factory.make_node()
        mac = factory.getRandomMACAddress()
        node.add_mac_address(mac)
        self.assertEqual(mac, node.get_primary_mac().mac_address)

    def test_get_primary_mac_returns_None_if_node_has_no_mac(self):
        node = factory.make_node()
        self.assertIsNone(node.get_primary_mac())

    def test_get_primary_mac_returns_oldest_mac(self):
        node = factory.make_node()
        macs = [factory.getRandomMACAddress() for counter in range(3)]
        offset = timedelta(0)
        for mac in macs:
            mac_address = node.add_mac_address(mac)
            mac_address.created += offset
            mac_address.save()
            offset += timedelta(1)
        self.assertEqual(macs[0], node.get_primary_mac().mac_address)

    def test_get_osystem_returns_default_osystem(self):
        node = factory.make_node(osystem='')
        osystem = Config.objects.get_config('default_osystem')
        self.assertEqual(osystem, node.get_osystem())

    def test_get_distro_series_returns_default_series(self):
        node = factory.make_node(distro_series='')
        series = Config.objects.get_config('default_distro_series')
        self.assertEqual(series, node.get_distro_series())

    def test_get_effective_license_key_returns_node_value(self):
        license_key = factory.make_name('license_key')
        node = factory.make_node(license_key=license_key)
        self.assertEqual(license_key, node.get_effective_license_key())

    def test_get_effective_license_key_returns_blank(self):
        node = factory.make_node()
        self.assertEqual('', node.get_effective_license_key())

    def test_get_effective_license_key_returns_global(self):
        license_key = factory.make_name('license_key')
        osystem = factory.make_name('os')
        series = factory.make_name('series')
        LicenseKey.objects.create(
            osystem=osystem, distro_series=series, license_key=license_key)
        node = factory.make_node(osystem=osystem, distro_series=series)
        self.assertEqual(license_key, node.get_effective_license_key())

    def test_delete_node_deletes_related_mac(self):
        node = factory.make_node()
        mac = node.add_mac_address('AA:BB:CC:DD:EE:FF')
        node.delete()
        self.assertRaises(
            MACAddress.DoesNotExist, MACAddress.objects.get, id=mac.id)

    def test_cannot_delete_allocated_node(self):
        node = factory.make_node(status=NODE_STATUS.ALLOCATED)
        self.assertRaises(NodeStateViolation, node.delete)

    def test_delete_node_also_deletes_related_static_IPs(self):
        # Prevent actual omshell commands from being called in tasks.
        self.patch(Omshell, 'remove')
        node = factory.make_node_with_mac_attached_to_nodegroupinterface()
        primary_mac = node.get_primary_mac()
        random_alloc_type = factory.pick_enum(
            IPADDRESS_TYPE, but_not=[IPADDRESS_TYPE.USER_RESERVED])
        primary_mac.claim_static_ips(alloc_type=random_alloc_type)
        node.delete()
        self.assertItemsEqual([], StaticIPAddress.objects.all())

    def test_delete_node_also_runs_task_to_delete_static_dhcp_maps(self):
        # Prevent actual omshell commands from being called in tasks.
        self.patch(Omshell, 'remove')
        node = factory.make_node_with_mac_attached_to_nodegroupinterface()
        primary_mac = node.get_primary_mac()
        primary_mac.claim_static_ips(alloc_type=IPADDRESS_TYPE.STICKY)
        node.delete()
        self.assertEqual(
            ['provisioningserver.tasks.remove_dhcp_host_map'],
            [task['task'].name for task in self.celery.tasks])

    def test_delete_node_also_deletes_dhcp_host_map(self):
        lease = make_active_lease()
        node = factory.make_node(nodegroup=lease.nodegroup)
        node.add_mac_address(lease.mac)
        # Prevent actual omshell commands from being called in the task.
        self.patch(Omshell, 'remove')
        node.delete()
        self.assertThat(
            self.celery.tasks[0]['kwargs'],
            Equals({
                'ip_address': lease.ip,
                'server_address': "127.0.0.1",
                'omapi_key': lease.nodegroup.dhcp_key,
                }))

    def test_delete_dynamic_host_maps_sends_to_correct_queue(self):
        lease = make_active_lease()
        node = factory.make_node(nodegroup=lease.nodegroup)
        node.add_mac_address(lease.mac)
        # Prevent actual omshell commands from being called in the task.
        self.patch(Omshell, 'remove')
        option_call = self.patch(celery.canvas.Signature, 'set')
        work_queue = node.work_queue
        node.delete()
        args, kwargs = option_call.call_args
        self.assertEqual(work_queue, kwargs['queue'])

    def test_delete_node_removes_multiple_host_maps(self):
        lease1 = make_active_lease()
        lease2 = make_active_lease(nodegroup=lease1.nodegroup)
        node = factory.make_node(nodegroup=lease1.nodegroup)
        node.add_mac_address(lease1.mac)
        node.add_mac_address(lease2.mac)
        # Prevent actual omshell commands from being called in the task.
        self.patch(Omshell, 'remove')
        node.delete()
        self.assertEqual(2, len(self.celery.tasks))

    def test_set_random_hostname_set_hostname(self):
        # Blank out enlistment_domain.
        Config.objects.set_config("enlistment_domain", '')
        node = factory.make_node()
        original_hostname = node.hostname
        node.set_random_hostname()
        self.assertNotEqual(original_hostname, node.hostname)
        self.assertNotEqual("", node.hostname)

    def test_set_random_hostname_checks_hostname_existence(self):
        Config.objects.set_config("enlistment_domain", '')
        existing_node = factory.make_node(hostname='hostname')

        hostnames = [existing_node.hostname, "new-hostname"]
        self.patch(
            node_module, "gen_candidate_names",
            lambda: iter(hostnames))

        node = factory.make_node()
        node.set_random_hostname()
        self.assertEqual('new-hostname', node.hostname)

    def test_get_effective_power_type_raises_if_not_set(self):
        node = factory.make_node(power_type='')
        self.assertRaises(
            node_module.UnknownPowerType, node.get_effective_power_type)

    def test_get_effective_power_type_reads_node_field(self):
        power_types = list(get_power_types().keys())  # Python3 proof.
        nodes = [
            factory.make_node(power_type=power_type)
            for power_type in power_types]
        self.assertEqual(
            power_types, [node.get_effective_power_type() for node in nodes])

    def test_power_parameters_are_stored(self):
        node = factory.make_node(power_type='')
        parameters = dict(user="tarquin", address="10.1.2.3")
        node.power_parameters = parameters
        node.save()
        node = reload_object(node)
        self.assertEqual(parameters, node.power_parameters)

    def test_power_parameters_default(self):
        node = factory.make_node(power_type='')
        self.assertEqual('', node.power_parameters)

    def test_get_effective_power_parameters_returns_power_parameters(self):
        params = {'test_parameter': factory.make_string()}
        node = factory.make_node(power_parameters=params)
        self.assertEqual(
            params['test_parameter'],
            node.get_effective_power_parameters()['test_parameter'])

    def test_get_effective_power_parameters_adds_system_id(self):
        node = factory.make_node()
        self.assertEqual(
            node.system_id,
            node.get_effective_power_parameters()['system_id'])

    def test_get_effective_power_parameters_adds_mac_if_no_params_set(self):
        node = factory.make_node()
        mac = factory.getRandomMACAddress()
        node.add_mac_address(mac)
        self.assertEqual(
            mac, node.get_effective_power_parameters()['mac_address'])

    def test_get_effective_power_parameters_adds_no_mac_if_params_set(self):
        node = factory.make_node(power_parameters={'foo': 'bar'})
        mac = factory.getRandomMACAddress()
        node.add_mac_address(mac)
        self.assertNotIn('mac', node.get_effective_power_parameters())

    def test_get_effective_power_parameters_adds_empty_power_off_mode(self):
        node = factory.make_node()
        params = node.get_effective_power_parameters()
        self.assertEqual("", params["power_off_mode"])

    def test_get_effective_power_type_no_default_power_address_if_not_virsh(
            self):
        node = factory.make_node(power_type="ether_wake")
        params = node.get_effective_power_parameters()
        self.assertEqual("", params["power_address"])

    def test_get_effective_power_type_defaults_power_address_if_virsh(self):
        node = factory.make_node(power_type="virsh")
        params = node.get_effective_power_parameters()
        self.assertEqual("qemu://localhost/system", params["power_address"])

    def test_get_effective_power_info_is_False_for_unset_power_type(self):
        node = factory.make_node(power_type="")
        self.assertEqual(
            (False, False, None, None),
            node.get_effective_power_info())

    def test_get_effective_power_info_is_True_for_set_power_type(self):
        node = factory.make_node(power_type=factory.make_name("pwr"))
        gepp = self.patch(node, "get_effective_power_parameters")
        gepp.return_value = sentinel.power_parameters
        self.assertEqual(
            (True, True, node.power_type, sentinel.power_parameters),
            node.get_effective_power_info())

    def test_get_effective_power_info_can_be_False_for_ether_wake(self):
        node = factory.make_node(power_type="ether_wake")
        gepp = self.patch(node, "get_effective_power_parameters")
        # When there's no MAC address in the power parameters,
        # get_effective_power_info() says that this node's power cannot
        # be turned on. However, it does return the power parameters.
        # For ether_wake the power can never be turned off.
        gepp.return_value = {}
        self.assertEqual(
            (False, False, "ether_wake", {}),
            node.get_effective_power_info())

    def test_get_effective_power_info_can_be_True_for_ether_wake(self):
        node = factory.make_node(power_type="ether_wake")
        gepp = self.patch(node, "get_effective_power_parameters")
        # When the MAC address is supplied it changes its mind: this
        # node's power can be turned on. For ether_wake the power can
        # never be turned off.
        gepp.return_value = {"mac_address": sentinel.mac_addr}
        self.assertEqual(
            (True, False, "ether_wake", {"mac_address": sentinel.mac_addr}),
            node.get_effective_power_info())

    def test_get_effective_power_info_returns_named_tuple(self):
        node = factory.make_node(power_type="ether_wake")
        # Ensure that can_be_started and can_be_stopped have different
        # values by specifying a MAC address for ether_wake.
        gepp = self.patch(node, "get_effective_power_parameters")
        gepp.return_value = {"mac_address": sentinel.mac_addr}
        self.assertThat(
            node.get_effective_power_info(),
            MatchesStructure.byEquality(
                can_be_started=True,
                can_be_stopped=False,
                power_type="ether_wake",
                power_parameters={
                    "mac_address": sentinel.mac_addr,
                },
            ),
        )

    def test_get_effective_kernel_options_with_nothing_set(self):
        node = factory.make_node()
        self.assertEqual((None, None), node.get_effective_kernel_options())

    def test_get_effective_kernel_options_sees_global_config(self):
        node = factory.make_node()
        kernel_opts = factory.make_string()
        Config.objects.set_config('kernel_opts', kernel_opts)
        self.assertEqual(
            (None, kernel_opts), node.get_effective_kernel_options())

    def test_get_effective_kernel_options_not_confused_by_None_opts(self):
        node = factory.make_node()
        tag = factory.make_tag()
        node.tags.add(tag)
        kernel_opts = factory.make_string()
        Config.objects.set_config('kernel_opts', kernel_opts)
        self.assertEqual(
            (None, kernel_opts), node.get_effective_kernel_options())

    def test_get_effective_kernel_options_not_confused_by_empty_str_opts(self):
        node = factory.make_node()
        tag = factory.make_tag(kernel_opts="")
        node.tags.add(tag)
        kernel_opts = factory.make_string()
        Config.objects.set_config('kernel_opts', kernel_opts)
        self.assertEqual(
            (None, kernel_opts), node.get_effective_kernel_options())

    def test_get_effective_kernel_options_multiple_tags_with_opts(self):
        # In this scenario:
        #     global   kernel_opts='fish-n-chips'
        #     tag_a    kernel_opts=null
        #     tag_b    kernel_opts=''
        #     tag_c    kernel_opts='bacon-n-eggs'
        # we require that 'bacon-n-eggs' is chosen as it is the first
        # tag with a valid kernel option.
        Config.objects.set_config('kernel_opts', 'fish-n-chips')
        node = factory.make_node()
        node.tags.add(factory.make_tag('tag_a'))
        node.tags.add(factory.make_tag('tag_b', kernel_opts=''))
        tag_c = factory.make_tag('tag_c', kernel_opts='bacon-n-eggs')
        node.tags.add(tag_c)

        self.assertEqual(
            (tag_c, 'bacon-n-eggs'), node.get_effective_kernel_options())

    def test_get_effective_kernel_options_ignores_unassociated_tag_value(self):
        node = factory.make_node()
        factory.make_tag(kernel_opts=factory.make_string())
        self.assertEqual((None, None), node.get_effective_kernel_options())

    def test_get_effective_kernel_options_uses_tag_value(self):
        node = factory.make_node()
        tag = factory.make_tag(kernel_opts=factory.make_string())
        node.tags.add(tag)
        self.assertEqual(
            (tag, tag.kernel_opts), node.get_effective_kernel_options())

    def test_get_effective_kernel_options_tag_overrides_global(self):
        node = factory.make_node()
        global_opts = factory.make_string()
        Config.objects.set_config('kernel_opts', global_opts)
        tag = factory.make_tag(kernel_opts=factory.make_string())
        node.tags.add(tag)
        self.assertEqual(
            (tag, tag.kernel_opts), node.get_effective_kernel_options())

    def test_get_effective_kernel_options_uses_first_real_tag_value(self):
        node = factory.make_node()
        # Intentionally create them in reverse order, so the default 'db' order
        # doesn't work, and we have asserted that we sort them.
        tag3 = factory.make_tag(
            factory.make_name('tag-03-'),
            kernel_opts=factory.make_string())
        tag2 = factory.make_tag(
            factory.make_name('tag-02-'),
            kernel_opts=factory.make_string())
        tag1 = factory.make_tag(factory.make_name('tag-01-'), kernel_opts=None)
        self.assertTrue(tag1.name < tag2.name)
        self.assertTrue(tag2.name < tag3.name)
        node.tags.add(tag1, tag2, tag3)
        self.assertEqual(
            (tag2, tag2.kernel_opts), node.get_effective_kernel_options())

    def test_acquire(self):
        node = factory.make_node(status=NODE_STATUS.READY)
        user = factory.make_user()
        token = create_auth_token(user)
        agent_name = factory.make_name('agent-name')
        node.acquire(user, token, agent_name)
        self.assertEqual(
            (user, NODE_STATUS.ALLOCATED, agent_name),
            (node.owner, node.status, node.agent_name))

    def test_release(self):
        agent_name = factory.make_name('agent-name')
        node = factory.make_node(
            status=NODE_STATUS.ALLOCATED, owner=factory.make_user(),
            agent_name=agent_name)
        node.release()
        self.assertEqual(
            (NODE_STATUS.READY, None, node.agent_name),
            (node.status, node.owner, ''))

    def test_release_deletes_static_ip_host_maps(self):
        user = factory.make_user()
        node = factory.make_node_with_mac_attached_to_nodegroupinterface(
            owner=user, status=NODE_STATUS.ALLOCATED)
        sips = node.get_primary_mac().claim_static_ips()
        delete_static_host_maps = self.patch(node, 'delete_static_host_maps')
        node.release()
        expected = [sip.ip.format() for sip in sips]
        self.assertThat(delete_static_host_maps, MockCalledOnceWith(expected))

    def test_delete_static_host_maps(self):
        user = factory.make_user()
        node = factory.make_node_with_mac_attached_to_nodegroupinterface(
            owner=user, status=NODE_STATUS.ALLOCATED)
        [sip] = node.get_primary_mac().claim_static_ips()
        self.patch(Omshell, 'remove')
        set_call = self.patch(celery.canvas.Signature, 'set')
        node.delete_static_host_maps([sip.ip.format()])
        self.assertThat(
            self.celery.tasks[0]['kwargs'],
            Equals({
                'ip_address': sip.ip.format(),
                'server_address': "127.0.0.1",
                'omapi_key': node.nodegroup.dhcp_key,
                }))
        args, kwargs = set_call.call_args
        self.assertEqual(node.work_queue, kwargs['queue'])

    def test_dynamic_ip_addresses_queries_leases(self):
        node = factory.make_node()
        macs = [factory.make_mac_address(node=node) for i in range(2)]
        leases = [
            factory.make_dhcp_lease(
                nodegroup=node.nodegroup, mac=mac.mac_address)
            for mac in macs]
        self.assertItemsEqual(
            [lease.ip for lease in leases], node.dynamic_ip_addresses())

    def test_dynamic_ip_addresses_uses_result_cache(self):
        # dynamic_ip_addresses has a specialized code path for the case where
        # the node group's set of DHCP leases is already cached in Django's
        # ORM.  This test exercises that code path.
        node = factory.make_node()
        macs = [factory.make_mac_address(node=node) for i in range(2)]
        leases = [
            factory.make_dhcp_lease(
                nodegroup=node.nodegroup, mac=mac.mac_address)
            for mac in macs]
        # Other nodes in the nodegroup have leases, but those are not
        # relevant here.
        factory.make_dhcp_lease(nodegroup=node.nodegroup)

        # Don't address the node directly; address it through a query with
        # prefetched DHCP leases, to ensure that the query cache for those
        # leases on the nodegroup will be populated.
        query = Node.objects.filter(id=node.id)
        query = query.prefetch_related('nodegroup__dhcplease_set')
        # The cache is populated.  This is the condition that triggers the
        # separate code path in Node.dynamic_ip_addresses().
        self.assertIsNotNone(
            query[0].nodegroup.dhcplease_set.all()._result_cache)

        # dynamic_ip_addresses() still returns the node's leased addresses.
        num_queries, addresses = count_queries(query[0].dynamic_ip_addresses)
        # It only takes one query: to get the node's MAC addresses.
        self.assertEqual(1, num_queries)
        # The result is not a query set, so this isn't hiding a further query.
        no_queries, _ = count_queries(list, addresses)
        self.assertEqual(0, no_queries)
        # We still get exactly the right IP addresses.
        self.assertItemsEqual([lease.ip for lease in leases], addresses)

    def test_dynamic_ip_addresses_filters_by_mac_addresses(self):
        node = factory.make_node()
        # Another node in the same nodegroup has some IP leases.  The one thing
        # that tells ip_addresses what nodes these leases belong to are their
        # MAC addresses.
        other_node = factory.make_node(nodegroup=node.nodegroup)
        macs = [factory.make_mac_address(node=node) for i in range(2)]
        for mac in macs:
            factory.make_dhcp_lease(
                nodegroup=node.nodegroup, mac=mac.mac_address)
        # The other node's leases do not get mistaken for ones that belong to
        # our original node.
        self.assertItemsEqual([], other_node.dynamic_ip_addresses())

    def test_static_ip_addresses_returns_static_ip_addresses(self):
        node = factory.make_node()
        [mac2, mac3] = [
            factory.make_mac_address(node=node) for i in range(2)]
        ip1 = factory.make_staticipaddress(mac=mac2)
        ip2 = factory.make_staticipaddress(mac=mac3)
        # Create another node with a static IP address.
        other_node = factory.make_node(nodegroup=node.nodegroup, mac=True)
        factory.make_staticipaddress(mac=other_node.macaddress_set.all()[0])
        self.assertItemsEqual([ip1.ip, ip2.ip], node.static_ip_addresses())

    def test_static_ip_addresses_uses_result_cache(self):
        # static_ip_addresses has a specialized code path for the case where
        # the node's static IPs are already cached in Django's ORM.  This
        # test exercises that code path.
        node = factory.make_node()
        [mac2, mac3] = [
            factory.make_mac_address(node=node) for i in range(2)]
        ip1 = factory.make_staticipaddress(mac=mac2)
        ip2 = factory.make_staticipaddress(mac=mac3)

        # Don't address the node directly; address it through a query with
        # prefetched static IPs, to ensure that the query cache for those
        # IP addresses.
        query = Node.objects.filter(id=node.id)
        query = query.prefetch_related('macaddress_set__ip_addresses')

        # dynamic_ip_addresses() still returns the node's leased addresses.
        num_queries, addresses = count_queries(query[0].static_ip_addresses)
        self.assertEqual(0, num_queries)
        # The result is not a query set, so this isn't hiding a further query.
        self.assertIsInstance(addresses, list)
        # We still get exactly the right IP addresses.
        self.assertItemsEqual([ip1.ip, ip2.ip], addresses)

    def test_ip_addresses_returns_static_ip_addresses_if_allocated(self):
        # If both static and dynamic IP addresses are present, the static
        # addresses take precedence: they are allocated and deallocated in
        # a synchronous fashion whereas the dynamic addresses are updated
        # periodically.
        node = factory.make_node(mac=True, disable_ipv4=False)
        mac = node.macaddress_set.all()[0]
        # Create a dynamic IP attached to the node.
        factory.make_dhcp_lease(
            nodegroup=node.nodegroup, mac=mac.mac_address)
        # Create a static IP attached to the node.
        ip = factory.make_staticipaddress(mac=mac)
        self.assertItemsEqual([ip.ip], node.ip_addresses())

    def test_ip_addresses_returns_dynamic_ip_if_no_static_ip(self):
        node = factory.make_node(mac=True, disable_ipv4=False)
        lease = factory.make_dhcp_lease(
            nodegroup=node.nodegroup,
            mac=node.macaddress_set.all()[0].mac_address)
        self.assertItemsEqual([lease.ip], node.ip_addresses())

    def test_ip_addresses_includes_static_ipv4_addresses_by_default(self):
        node = factory.make_node(disable_ipv4=False)
        ipv4_address = factory.getRandomIPAddress()
        ipv6_address = factory.make_ipv6_address()
        self.patch(node, 'static_ip_addresses').return_value = [
            ipv4_address,
            ipv6_address,
            ]
        self.assertItemsEqual(
            [ipv4_address, ipv6_address],
            node.ip_addresses())

    def test_ip_addresses_includes_dynamic_ipv4_addresses_by_default(self):
        node = factory.make_node(disable_ipv4=False)
        ipv4_address = factory.getRandomIPAddress()
        ipv6_address = factory.make_ipv6_address()
        self.patch(node, 'dynamic_ip_addresses').return_value = [
            ipv4_address,
            ipv6_address,
            ]
        self.assertItemsEqual(
            [ipv4_address, ipv6_address],
            node.ip_addresses())

    def test_ip_addresses_strips_static_ipv4_addresses_if_ipv4_disabled(self):
        node = factory.make_node(disable_ipv4=True)
        ipv4_address = factory.getRandomIPAddress()
        ipv6_address = factory.make_ipv6_address()
        self.patch(node, 'static_ip_addresses').return_value = [
            ipv4_address,
            ipv6_address,
            ]
        self.assertEqual([ipv6_address], node.ip_addresses())

    def test_ip_addresses_strips_dynamic_ipv4_addresses_if_ipv4_disabled(self):
        node = factory.make_node(disable_ipv4=True)
        ipv4_address = factory.getRandomIPAddress()
        ipv6_address = factory.make_ipv6_address()
        self.patch(node, 'dynamic_ip_addresses').return_value = [
            ipv4_address,
            ipv6_address,
            ]
        self.assertEqual([ipv6_address], node.ip_addresses())

    def test_release_turns_on_netboot(self):
        node = factory.make_node(
            status=NODE_STATUS.ALLOCATED, owner=factory.make_user())
        node.set_netboot(on=False)
        node.release()
        self.assertTrue(node.netboot)

    def test_release_clears_osystem_and_distro_series(self):
        node = factory.make_node(
            status=NODE_STATUS.ALLOCATED, owner=factory.make_user())
        osystem = factory.pick_OS()
        release = factory.pick_release(osystem)
        node.osystem = osystem.name
        node.distro_series = release
        node.release()
        self.assertEqual("", node.osystem)
        self.assertEqual("", node.distro_series)

    def test_release_powers_off_node(self):
        # Test that releasing a node causes a 'power_off' celery job.
        node = factory.make_node(
            status=NODE_STATUS.ALLOCATED, owner=factory.make_user(),
            power_type='virsh')
        # Prevent actual job script from running.
        self.patch(PowerAction, 'run_shell', lambda *args, **kwargs: ('', ''))
        node.release()
        self.assertEqual(
            (1, 'provisioningserver.tasks.power_off'),
            (len(self.celery.tasks), self.celery.tasks[0]['task'].name))

    def test_release_deallocates_static_ips(self):
        deallocate = self.patch(StaticIPAddressManager, 'deallocate_by_node')
        node = factory.make_node(
            status=NODE_STATUS.ALLOCATED, owner=factory.make_user(),
            power_type='ether_wake')
        node.release()
        self.assertThat(deallocate, MockCalledOnceWith(node))

    def test_release_updates_dns(self):
        change_dns_zones = self.patch(dns_config, 'change_dns_zones')
        nodegroup = factory.make_node_group(
            management=NODEGROUPINTERFACE_MANAGEMENT.DHCP_AND_DNS,
            status=NODEGROUP_STATUS.ACCEPTED)
        node = factory.make_node(
            nodegroup=nodegroup, status=NODE_STATUS.ALLOCATED,
            owner=factory.make_user(), power_type='ether_wake')
        node.release()
        self.assertThat(change_dns_zones, MockCalledOnceWith([node.nodegroup]))

    def test_accept_enlistment_gets_node_out_of_declared_state(self):
        # If called on a node in New state, accept_enlistment()
        # changes the node's status, and returns the node.
        target_state = NODE_STATUS.COMMISSIONING

        node = factory.make_node(status=NODE_STATUS.NEW)
        return_value = node.accept_enlistment(factory.make_user())
        self.assertEqual((node, target_state), (return_value, node.status))

    def test_accept_enlistment_does_nothing_if_already_accepted(self):
        # If a node has already been accepted, but not assigned a role
        # yet, calling accept_enlistment on it is meaningless but not an
        # error.  The method returns None in this case.
        accepted_states = [
            NODE_STATUS.COMMISSIONING,
            NODE_STATUS.READY,
            ]
        nodes = {
            status: factory.make_node(status=status)
            for status in accepted_states}

        return_values = {
            status: node.accept_enlistment(factory.make_user())
            for status, node in nodes.items()}

        self.assertEqual(
            {status: None for status in accepted_states}, return_values)
        self.assertEqual(
            {status: status for status in accepted_states},
            {status: node.status for status, node in nodes.items()})

    def test_accept_enlistment_rejects_bad_state_change(self):
        # If a node is neither New nor in one of the "accepted"
        # states where acceptance is a safe no-op, accept_enlistment
        # raises a node state violation and leaves the node's state
        # unchanged.
        all_states = map_enum(NODE_STATUS).values()
        acceptable_states = [
            NODE_STATUS.NEW,
            NODE_STATUS.COMMISSIONING,
            NODE_STATUS.READY,
            ]
        unacceptable_states = set(all_states) - set(acceptable_states)
        nodes = {
            status: factory.make_node(status=status)
            for status in unacceptable_states}

        exceptions = {status: False for status in unacceptable_states}
        for status, node in nodes.items():
            try:
                node.accept_enlistment(factory.make_user())
            except NodeStateViolation:
                exceptions[status] = True

        self.assertEqual(
            {status: True for status in unacceptable_states}, exceptions)
        self.assertEqual(
            {status: status for status in unacceptable_states},
            {status: node.status for status, node in nodes.items()})

    def test_start_commissioning_changes_status_and_starts_node(self):
        start_nodes = self.patch(Node.objects, "start_nodes")

        node = factory.make_node(
            status=NODE_STATUS.NEW, power_type='ether_wake')
        factory.make_mac_address(node=node)
        admin = factory.make_admin()
        node.start_commissioning(admin)

        expected_attrs = {
            'status': NODE_STATUS.COMMISSIONING,
        }
        self.assertAttributes(node, expected_attrs)
        self.assertThat(start_nodes, MockCalledOnceWith(
            [node.system_id], admin, user_data=ANY))

    def test_start_commisssioning_doesnt_start_nodes_for_non_admin_users(self):
        node = factory.make_node(
            status=NODE_STATUS.NEW, power_type='ether_wake')
        factory.make_mac_address(node=node)
        node.start_commissioning(factory.make_user())

        expected_attrs = {
            'status': NODE_STATUS.COMMISSIONING,
        }
        self.assertAttributes(node, expected_attrs)
        self.assertEqual([], self.celery.tasks)

    def test_start_commissioning_sets_user_data(self):
        start_nodes = self.patch(Node.objects, "start_nodes")

        node = factory.make_node(status=NODE_STATUS.NEW)
        user_data = factory.make_string().encode('ascii')
        generate_user_data = self.patch(
            commissioning.user_data, 'generate_user_data')
        generate_user_data.return_value = user_data
        admin = factory.make_admin()
        node.start_commissioning(admin)
        self.assertThat(start_nodes, MockCalledOnceWith(
            [node.system_id], admin, user_data=user_data))

    def test_start_commissioning_clears_node_commissioning_results(self):
        node = factory.make_node(status=NODE_STATUS.NEW)
        NodeResult.objects.store_data(
            node, factory.make_string(),
            random.randint(0, 10),
            RESULT_TYPE.COMMISSIONING,
            Bin(factory.make_bytes()))
        node.start_commissioning(factory.make_admin())
        self.assertItemsEqual([], node.noderesult_set.all())

    def test_start_commissioning_ignores_other_commissioning_results(self):
        node = factory.make_node()
        filename = factory.make_string()
        data = factory.make_bytes()
        script_result = random.randint(0, 10)
        NodeResult.objects.store_data(
            node, filename, script_result, RESULT_TYPE.COMMISSIONING,
            Bin(data))
        other_node = factory.make_node(status=NODE_STATUS.NEW)
        other_node.start_commissioning(factory.make_admin())
        self.assertEqual(
            data, NodeResult.objects.get_data(node, filename))

    def test_abort_commissioning_changes_status_and_stops_node(self):
        self.patch(PowerAction, 'run_shell').return_value = ('', '')
        node = factory.make_node(
            status=NODE_STATUS.COMMISSIONING, power_type='virsh')
        node.abort_commissioning(factory.make_admin())
        expected_attrs = {
            'status': NODE_STATUS.NEW,
        }
        self.assertAttributes(node, expected_attrs)
        self.assertEqual(
            ['provisioningserver.tasks.power_off'],
            [task['task'].name for task in self.celery.tasks])

    def test_abort_commisssioning_doesnt_stop_nodes_for_non_admin_users(self):
        node = factory.make_node(
            status=NODE_STATUS.COMMISSIONING, power_type='virsh')
        node.abort_commissioning(factory.make_user())
        expected_attrs = {
            'status': NODE_STATUS.COMMISSIONING,
        }
        self.assertAttributes(node, expected_attrs)
        self.assertEqual([], self.celery.tasks)

    def test_abort_commisssioning_errors_if_node_is_not_commissioning(self):
        unaccepted_statuses = set(map_enum(NODE_STATUS).values())
        unaccepted_statuses.remove(NODE_STATUS.COMMISSIONING)
        for status in unaccepted_statuses:
            node = factory.make_node(
                status=status, power_type='virsh')
            self.assertRaises(
                NodeStateViolation, node.abort_commissioning,
                factory.make_admin())

    def test_full_clean_checks_status_transition_and_raises_if_invalid(self):
        # RETIRED -> ALLOCATED is an invalid transition.
        node = factory.make_node(
            status=NODE_STATUS.RETIRED, owner=factory.make_user())
        node.status = NODE_STATUS.ALLOCATED
        self.assertRaisesRegexp(
            NodeStateViolation,
            "Invalid transition: Retired -> Allocated.",
            node.full_clean)

    def test_full_clean_passes_if_status_unchanged(self):
        status = factory.pick_choice(NODE_STATUS_CHOICES)
        node = factory.make_node(status=status)
        node.status = status
        node.full_clean()
        # The test is that this does not raise an error.
        pass

    def test_full_clean_passes_if_status_valid_transition(self):
        # NODE_STATUS.READY -> NODE_STATUS.ALLOCATED is a valid
        # transition.
        status = NODE_STATUS.READY
        node = factory.make_node(status=status)
        node.status = NODE_STATUS.ALLOCATED
        node.full_clean()
        # The test is that this does not raise an error.
        pass

    def test_save_raises_node_state_violation_on_bad_transition(self):
        # RETIRED -> ALLOCATED is an invalid transition.
        node = factory.make_node(
            status=NODE_STATUS.RETIRED, owner=factory.make_user())
        node.status = NODE_STATUS.ALLOCATED
        self.assertRaisesRegexp(
            NodeStateViolation,
            "Invalid transition: Retired -> Allocated.",
            node.save)

    def test_netboot_defaults_to_True(self):
        node = Node()
        self.assertTrue(node.netboot)

    def test_nodegroup_cannot_be_null(self):
        node = factory.make_node()
        node.nodegroup = None
        self.assertRaises(ValidationError, node.save)

    def test_fqdn_returns_hostname_if_dns_not_managed(self):
        nodegroup = factory.make_node_group(
            name=factory.make_string(),
            management=NODEGROUPINTERFACE_MANAGEMENT.DHCP)
        hostname_with_domain = '%s.%s' % (
            factory.make_string(), factory.make_string())
        node = factory.make_node(
            nodegroup=nodegroup, hostname=hostname_with_domain)
        self.assertEqual(hostname_with_domain, node.fqdn)

    def test_fqdn_replaces_hostname_if_dns_is_managed(self):
        hostname_without_domain = factory.make_name('hostname')
        hostname_with_domain = '%s.%s' % (
            hostname_without_domain, factory.make_string())
        domain = factory.make_name('domain')
        nodegroup = factory.make_node_group(
            status=NODEGROUP_STATUS.ACCEPTED,
            name=domain,
            management=NODEGROUPINTERFACE_MANAGEMENT.DHCP_AND_DNS)
        node = factory.make_node(
            hostname=hostname_with_domain, nodegroup=nodegroup)
        expected_hostname = '%s.%s' % (hostname_without_domain, domain)
        self.assertEqual(expected_hostname, node.fqdn)

    def test_boot_type_has_fastpath_set_by_default(self):
        node = factory.make_node()
        self.assertEqual(NODE_BOOT.FASTPATH, node.boot_type)

    def test_split_arch_returns_arch_as_tuple(self):
        main_arch = factory.make_name('arch')
        sub_arch = factory.make_name('subarch')
        full_arch = '%s/%s' % (main_arch, sub_arch)
        node = factory.make_node(architecture=full_arch)
        self.assertEqual((main_arch, sub_arch), node.split_arch())

    def test_mac_addresses_on_managed_interfaces_returns_only_managed(self):
        node = factory.make_node_with_mac_attached_to_nodegroupinterface(
            management=NODEGROUPINTERFACE_MANAGEMENT.DHCP)

        mac_with_no_interface = factory.make_mac_address(node=node)
        unmanaged_interface = factory.make_node_group_interface(
            nodegroup=node.nodegroup,
            management=NODEGROUPINTERFACE_MANAGEMENT.UNMANAGED)
        mac_with_unmanaged_interface = factory.make_mac_address(
            node=node, cluster_interface=unmanaged_interface)
        ignore_unused(mac_with_no_interface, mac_with_unmanaged_interface)

        observed = node.mac_addresses_on_managed_interfaces()
        self.assertItemsEqual([node.get_primary_mac()], observed)

    def test_mac_addresses_on_managed_interfaces_returns_empty_if_none(self):
        node = factory.make_node(mac=True)
        observed = node.mac_addresses_on_managed_interfaces()
        self.assertItemsEqual([], observed)

    failed_statuses_mapping = {
        NODE_STATUS.COMMISSIONING: NODE_STATUS.FAILED_COMMISSIONING,
        NODE_STATUS.DEPLOYING: NODE_STATUS.FAILED_DEPLOYMENT,
    }

    def test_mark_failed_updates_status(self):
        nodes_mapping = {
            status: factory.make_node(status=status)
            for status in self.failed_statuses_mapping
        }
        for node in nodes_mapping.values():
            node.mark_failed(factory.make_name('error-description'))
        self.assertEqual(
            self.failed_statuses_mapping,
            {status: node.status for status, node in nodes_mapping.items()})

    def test_mark_failed_updates_error_description(self):
        node = factory.make_node(status=NODE_STATUS.COMMISSIONING)
        description = factory.make_name('error-description')
        node.mark_failed(description)
        self.assertEqual(description, reload_object(node).error_description)

    def test_mark_failed_raises_for_unauthorized_node_status(self):
        status = factory.pick_choice(
            NODE_STATUS_CHOICES,
            but_not=self.failed_statuses_mapping.keys())
        node = factory.make_node(status=status)
        description = factory.make_name('error-description')
        self.assertRaises(NodeStateViolation, node.mark_failed, description)

    def test_mark_broken_changes_status_to_broken(self):
        node = factory.make_node(
            status=NODE_STATUS.NEW, owner=factory.make_user())
        node.mark_broken(factory.make_name('error-description'))
        self.assertEqual(NODE_STATUS.BROKEN, reload_object(node).status)

    def test_mark_broken_releases_allocated_node(self):
        node = factory.make_node(
            status=NODE_STATUS.ALLOCATED, owner=factory.make_user())
        node.mark_broken(factory.make_name('error-description'))
        node = reload_object(node)
        self.assertEqual(
            (NODE_STATUS.BROKEN, None), (node.status, node.owner))

    def test_mark_fixed_changes_status(self):
        node = factory.make_node(status=NODE_STATUS.BROKEN)
        node.mark_fixed()
        self.assertEqual(NODE_STATUS.READY, reload_object(node).status)

    def test_mark_fixed_updates_error_description(self):
        description = factory.make_name('error-description')
        node = factory.make_node(
            status=NODE_STATUS.BROKEN, error_description=description)
        node.mark_fixed()
        self.assertEqual('', reload_object(node).error_description)

    def test_mark_fixed_fails_if_node_isnt_broken(self):
        status = factory.pick_choice(
            NODE_STATUS_CHOICES, but_not=[NODE_STATUS.BROKEN])
        node = factory.make_node(status=status)
        self.assertRaises(NodeStateViolation, node.mark_fixed)

    def test_update_power_state(self):
        node = factory.make_node()
        state = factory.pick_enum(POWER_STATE)
        node.update_power_state(state)
        self.assertEqual(state, reload_object(node).power_state)

    def test_end_deployment_changes_state(self):
        node = factory.make_node(status=NODE_STATUS.DEPLOYING)
        node.end_deployment()
        self.assertEqual(NODE_STATUS.DEPLOYED, reload_object(node).status)

    def test_start_deployment_changes_state(self):
        node = factory.make_node(status=NODE_STATUS.ALLOCATED)
        node.start_deployment()
        self.assertEqual(NODE_STATUS.DEPLOYING, reload_object(node).status)


class NodeRoutersTest(MAASServerTestCase):

    def test_routers_stores_mac_address(self):
        node = factory.make_node()
        macs = [MAC('aa:bb:cc:dd:ee:ff')]
        node.routers = macs
        node.save()
        self.assertEqual(macs, reload_object(node).routers)

    def test_routers_stores_multiple_mac_addresses(self):
        node = factory.make_node()
        macs = [MAC('aa:bb:cc:dd:ee:ff'), MAC('00:11:22:33:44:55')]
        node.routers = macs
        node.save()
        self.assertEqual(macs, reload_object(node).routers)

    def test_routers_can_append(self):
        node = factory.make_node()
        mac1 = MAC('aa:bb:cc:dd:ee:ff')
        mac2 = MAC('00:11:22:33:44:55')
        node.routers = [mac1]
        node.save()
        node = reload_object(node)
        node.routers.append(mac2)
        node.save()
        self.assertEqual([mac1, mac2], reload_object(node).routers)


class NodeTransitionsTests(MAASServerTestCase):
    """Test the structure of NODE_TRANSITIONS."""

    def test_NODE_TRANSITIONS_initial_states(self):
        allowed_states = set(NODE_STATUS_CHOICES_DICT.keys() + [None])

        self.assertTrue(set(NODE_TRANSITIONS.keys()) <= allowed_states)

    def test_NODE_TRANSITIONS_destination_state(self):
        all_destination_states = []
        for destination_states in NODE_TRANSITIONS.values():
            all_destination_states.extend(destination_states)
        allowed_states = set(NODE_STATUS_CHOICES_DICT.keys())

        self.assertTrue(set(all_destination_states) <= allowed_states)


class NodeManagerTest(MAASServerTestCase):

    def make_node(self, user=None, **kwargs):
        """Create a node, allocated to `user` if given."""
        if user is None:
            status = NODE_STATUS.READY
        else:
            status = NODE_STATUS.ALLOCATED
        return factory.make_node(status=status, owner=user, **kwargs)

    def make_node_with_mac(self, user=None, **kwargs):
        node = self.make_node(user, **kwargs)
        mac = factory.make_mac_address(node=node)
        return node, mac

    def make_user_data(self):
        """Create a blob of arbitrary user-data."""
        return factory.make_string().encode('ascii')

    def test_filter_by_ids_filters_nodes_by_ids(self):
        nodes = [factory.make_node() for counter in range(5)]
        ids = [node.system_id for node in nodes]
        selection = slice(1, 3)
        self.assertItemsEqual(
            nodes[selection],
            Node.objects.filter_by_ids(Node.objects.all(), ids[selection]))

    def test_filter_by_ids_with_empty_list_returns_empty(self):
        factory.make_node()
        self.assertItemsEqual(
            [], Node.objects.filter_by_ids(Node.objects.all(), []))

    def test_filter_by_ids_without_ids_returns_full(self):
        node = factory.make_node()
        self.assertItemsEqual(
            [node], Node.objects.filter_by_ids(Node.objects.all(), None))

    def test_get_nodes_for_user_lists_visible_nodes(self):
        """get_nodes with perm=NODE_PERMISSION.VIEW lists the nodes a user
        has access to.

        When run for a regular user it returns unowned nodes, and nodes
        owned by that user.
        """
        user = factory.make_user()
        visible_nodes = [self.make_node(owner) for owner in [None, user]]
        self.make_node(factory.make_user())
        self.assertItemsEqual(
            visible_nodes, Node.objects.get_nodes(user, NODE_PERMISSION.VIEW))

    def test_get_nodes_admin_lists_all_nodes(self):
        admin = factory.make_admin()
        owners = [
            None,
            factory.make_user(),
            factory.make_admin(),
            admin,
            ]
        nodes = [self.make_node(owner) for owner in owners]
        self.assertItemsEqual(
            nodes, Node.objects.get_nodes(admin, NODE_PERMISSION.VIEW))

    def test_get_nodes_filters_by_id(self):
        user = factory.make_user()
        nodes = [self.make_node(user) for counter in range(5)]
        ids = [node.system_id for node in nodes]
        wanted_slice = slice(0, 3)
        self.assertItemsEqual(
            nodes[wanted_slice],
            Node.objects.get_nodes(
                user, NODE_PERMISSION.VIEW, ids=ids[wanted_slice]))

    def test_get_nodes_filters_from_nodes(self):
        admin = factory.make_admin()
        # Node that we want to see in the result:
        wanted_node = factory.make_node()
        # Node that we'll exclude from from_nodes:
        factory.make_node()

        self.assertItemsEqual(
            [wanted_node],
            Node.objects.get_nodes(
                admin, NODE_PERMISSION.VIEW,
                from_nodes=Node.objects.filter(id=wanted_node.id)))

    def test_get_nodes_combines_from_nodes_with_other_filter(self):
        user = factory.make_user()
        # Node that we want to see in the result:
        matching_node = factory.make_node(owner=user)
        # Node that we'll exclude from from_nodes:
        factory.make_node(owner=user)
        # Node that will be ignored on account of belonging to someone else:
        invisible_node = factory.make_node(owner=factory.make_user())

        self.assertItemsEqual(
            [matching_node],
            Node.objects.get_nodes(
                user, NODE_PERMISSION.VIEW,
                from_nodes=Node.objects.filter(id__in=(
                    matching_node.id,
                    invisible_node.id,
                    ))))

    def test_get_nodes_with_edit_perm_for_user_lists_owned_nodes(self):
        user = factory.make_user()
        visible_node = self.make_node(user)
        self.make_node(None)
        self.make_node(factory.make_user())
        self.assertItemsEqual(
            [visible_node],
            Node.objects.get_nodes(user, NODE_PERMISSION.EDIT))

    def test_get_nodes_with_edit_perm_admin_lists_all_nodes(self):
        admin = factory.make_admin()
        owners = [
            None,
            factory.make_user(),
            factory.make_admin(),
            admin,
            ]
        nodes = [self.make_node(owner) for owner in owners]
        self.assertItemsEqual(
            nodes, Node.objects.get_nodes(admin, NODE_PERMISSION.EDIT))

    def test_get_nodes_with_admin_perm_returns_empty_list_for_user(self):
        user = factory.make_user()
        [self.make_node(user) for counter in range(5)]
        self.assertItemsEqual(
            [],
            Node.objects.get_nodes(user, NODE_PERMISSION.ADMIN))

    def test_get_nodes_with_admin_perm_returns_all_nodes_for_admin(self):
        user = factory.make_user()
        nodes = [self.make_node(user) for counter in range(5)]
        self.assertItemsEqual(
            nodes,
            Node.objects.get_nodes(
                factory.make_admin(), NODE_PERMISSION.ADMIN))

    def test_get_visible_node_or_404_ok(self):
        """get_node_or_404 fetches nodes by system_id."""
        user = factory.make_user()
        node = self.make_node(user)
        self.assertEqual(
            node,
            Node.objects.get_node_or_404(
                node.system_id, user, NODE_PERMISSION.VIEW))

    def test_get_available_nodes_finds_available_nodes(self):
        user = factory.make_user()
        node1 = self.make_node(None)
        node2 = self.make_node(None)
        self.assertItemsEqual(
            [node1, node2],
            Node.objects.get_available_nodes_for_acquisition(user))

    def test_get_available_node_returns_empty_list_if_empty(self):
        user = factory.make_user()
        self.assertEqual(
            [], list(Node.objects.get_available_nodes_for_acquisition(user)))

    def test_get_available_nodes_ignores_taken_nodes(self):
        user = factory.make_user()
        available_status = NODE_STATUS.READY
        unavailable_statuses = (
            set(NODE_STATUS_CHOICES_DICT) - set([available_status]))
        for status in unavailable_statuses:
            factory.make_node(status=status)
        self.assertEqual(
            [], list(Node.objects.get_available_nodes_for_acquisition(user)))

    def test_get_available_node_ignores_invisible_nodes(self):
        user = factory.make_user()
        node = self.make_node()
        node.owner = factory.make_user()
        node.save()
        self.assertEqual(
            [], list(Node.objects.get_available_nodes_for_acquisition(user)))

    def test_stop_nodes_stops_nodes(self):
        # We don't actually want to fire off power events, but we'll go
        # through the motions right up to the point where we'd normally
        # run shell commands.
        self.patch(PowerAction, 'run_shell', lambda *args, **kwargs: ('', ''))
        user = factory.make_user()
        node, mac = self.make_node_with_mac(user, power_type='virsh')
        output = Node.objects.stop_nodes([node.system_id], user)

        self.assertItemsEqual([node], output)
        self.assertEqual(
            (1, 'provisioningserver.tasks.power_off'),
            (
                len(self.celery.tasks),
                self.celery.tasks[0]['task'].name,
            ))

    def test_stop_nodes_task_routed_to_nodegroup_worker(self):
        user = factory.make_user()
        node, mac = self.make_node_with_mac(user, power_type='virsh')
        task = self.patch(node_module, 'power_off')
        Node.objects.stop_nodes([node.system_id], user)
        args, kwargs = task.apply_async.call_args
        self.assertEqual(node.work_queue, kwargs['queue'])

    def test_stop_nodes_task_uses_stop_mode(self):
        self.patch(PowerAction, 'run_shell').return_value = ('', '')
        user = factory.make_user()
        node, mac = self.make_node_with_mac(user, power_type='virsh')
        stop_mode = factory.make_name('stop_mode')
        Node.objects.stop_nodes([node.system_id], user, stop_mode=stop_mode)
        self.assertEqual(
            stop_mode,
            self.celery.tasks[0]['kwargs']['power_off_mode'])

    def test_stop_nodes_ignores_uneditable_nodes(self):
        nodes = [
            self.make_node_with_mac(
                factory.make_user(), power_type='ether_wake')
            for counter in range(3)]
        ids = [node.system_id for node, mac in nodes]
        stoppable_node = nodes[0][0]
        self.assertItemsEqual(
            [stoppable_node],
            Node.objects.stop_nodes(ids, stoppable_node.owner))

    def test_stop_nodes_does_not_attempt_power_task_if_no_power_type(self):
        # If the node has a power_type set to UNKNOWN_POWER_TYPE
        # NodeManager.stop_node(this_node) won't create a power event
        # for it.
        user = factory.make_user()
        node, unused = self.make_node_with_mac(
            user, power_type='')
        output = Node.objects.stop_nodes([node.system_id], user)

        self.assertItemsEqual([], output)
        self.assertEqual(0, len(self.celery.tasks))

    def test_netboot_on(self):
        node = factory.make_node(netboot=False)
        node.set_netboot(True)
        self.assertTrue(node.netboot)

    def test_netboot_off(self):
        node = factory.make_node(netboot=True)
        node.set_netboot(False)
        self.assertFalse(node.netboot)


class NodeManagerTest_StartNodes(MAASServerTestCase):

    def setUp(self):
        super(NodeManagerTest_StartNodes, self).setUp()
        self.useFixture(RegionEventLoopFixture("rpc"))
        self.useFixture(RunningEventLoopFixture())
        self.rpc_fixture = self.useFixture(MockLiveRegionToClusterRPCFixture())

    def prepare_rpc_to_cluster(self, nodegroup):
        protocol = self.rpc_fixture.makeCluster(
            nodegroup, cluster.CreateHostMaps, cluster.PowerOn)
        protocol.CreateHostMaps.side_effect = always_succeed_with({})
        protocol.PowerOn.side_effect = always_succeed_with({})
        return protocol

    def make_acquired_nodes_with_macs(self, user, nodegroup=None, count=3):
        nodes = []
        for _ in xrange(count):
            node = factory.make_node_with_mac_attached_to_nodegroupinterface(
                nodegroup=nodegroup, status=NODE_STATUS.READY)
            self.prepare_rpc_to_cluster(node.nodegroup)
            node.acquire(user)
            nodes.append(node)
        return nodes

    def test__sets_user_data(self):
        user = factory.make_user()
        nodegroup = factory.make_node_group()
        self.prepare_rpc_to_cluster(nodegroup)
        nodes = self.make_acquired_nodes_with_macs(user, nodegroup)
        user_data = factory.make_bytes()

        with TwistedLoggerFixture() as twisted_log:
            Node.objects.start_nodes(
                list(node.system_id for node in nodes),
                user, user_data=user_data)

        # All three nodes have been given the same user data.
        nuds = NodeUserData.objects.filter(
            node_id__in=(node.id for node in nodes))
        self.assertEqual({user_data}, {nud.data for nud in nuds})
        # No complaints are made to the Twisted log.
        self.assertEqual("", twisted_log.dump())

    def test__resets_user_data(self):
        user = factory.make_user()
        nodegroup = factory.make_node_group()
        self.prepare_rpc_to_cluster(nodegroup)
        nodes = self.make_acquired_nodes_with_macs(user, nodegroup)

        with TwistedLoggerFixture() as twisted_log:
            Node.objects.start_nodes(
                list(node.system_id for node in nodes),
                user, user_data=None)

        # All three nodes have been given the same user data.
        nuds = NodeUserData.objects.filter(
            node_id__in=(node.id for node in nodes))
        self.assertThat(list(nuds), HasLength(0))
        # No complaints are made to the Twisted log.
        self.assertEqual("", twisted_log.dump())

    def test__claims_static_ip_addresses(self):
        user = factory.make_user()
        nodegroup = factory.make_node_group()
        self.prepare_rpc_to_cluster(nodegroup)
        nodes = self.make_acquired_nodes_with_macs(user, nodegroup)

        from maastesting.matchers import MockAnyCall

        claim_static_ip_addresses = self.patch_autospec(
            Node, "claim_static_ip_addresses", spec_set=False)
        claim_static_ip_addresses.return_value = {}

        with TwistedLoggerFixture() as twisted_log:
            Node.objects.start_nodes(
                list(node.system_id for node in nodes), user)

        for node in nodes:
            self.expectThat(claim_static_ip_addresses, MockAnyCall(node))
        # No complaints are made to the Twisted log.
        self.assertEqual("", twisted_log.dump())

    def test__claims_static_ip_addresses_for_allocated_nodes_only(self):
        user = factory.make_user()
        nodegroup = factory.make_node_group()
        self.prepare_rpc_to_cluster(nodegroup)
        nodes = self.make_acquired_nodes_with_macs(user, nodegroup, count=2)

        # Change the status of the first node to something other than
        # allocated.
        broken_node, allocated_node = nodes
        broken_node.status = NODE_STATUS.BROKEN
        broken_node.save()

        claim_static_ip_addresses = self.patch_autospec(
            Node, "claim_static_ip_addresses", spec_set=False)
        claim_static_ip_addresses.return_value = {}

        with TwistedLoggerFixture() as twisted_log:
            Node.objects.start_nodes(
                list(node.system_id for node in nodes), user)

        # Only one call is made to claim_static_ip_addresses(), for the
        # still-allocated node.
        self.assertThat(
            claim_static_ip_addresses,
            MockCalledOnceWith(allocated_node))
        # No complaints are made to the Twisted log.
        self.assertEqual("", twisted_log.dump())

    def test__updates_host_maps(self):
        user = factory.make_user()
        nodes = self.make_acquired_nodes_with_macs(user)

        update_host_maps = self.patch(node_module, "update_host_maps")
        update_host_maps.return_value = []  # No failures.

        with TwistedLoggerFixture() as twisted_log:
            Node.objects.start_nodes(
                list(node.system_id for node in nodes), user)

        # Host maps are updated.
        self.assertThat(
            update_host_maps, MockCalledOnceWith({
                node.nodegroup: {
                    ip_address.ip: mac.mac_address
                    for ip_address in mac.ip_addresses.all()
                }
                for node in nodes
                for mac in node.mac_addresses_on_managed_interfaces()
            }))
        # No complaints are made to the Twisted log.
        self.assertEqual("", twisted_log.dump())

    def test__propagates_errors_when_updating_host_maps(self):
        user = factory.make_user()
        nodes = self.make_acquired_nodes_with_macs(user)

        update_host_maps = self.patch(node_module, "update_host_maps")
        update_host_maps.return_value = [
            Failure(AssertionError("That is so not true")),
            Failure(ZeroDivisionError("I cannot defy mathematics")),
        ]

        with TwistedLoggerFixture() as twisted_log:
            error = self.assertRaises(
                MultipleFailures, Node.objects.start_nodes,
                list(node.system_id for node in nodes), user)

        self.assertSequenceEqual(
            update_host_maps.return_value, error.args)

        # No complaints are made to the Twisted log.
        self.assertEqual("", twisted_log.dump())

    def test__updates_dns(self):
        user = factory.make_user()
        nodes = self.make_acquired_nodes_with_macs(user)

        change_dns_zones = self.patch(dns_config, "change_dns_zones")

        with TwistedLoggerFixture() as twisted_log:
            Node.objects.start_nodes(
                list(node.system_id for node in nodes), user)

        self.assertThat(
            change_dns_zones, MockCalledOnceWith(
                {node.nodegroup for node in nodes}))

        # No complaints are made to the Twisted log.
        self.assertEqual("", twisted_log.dump())

    def test__starts_nodes(self):
        user = factory.make_user()
        nodes = self.make_acquired_nodes_with_macs(user)
        power_infos = list(
            node.get_effective_power_info()
            for node in nodes)

        power_on_nodes = self.patch(node_module, "power_on_nodes")
        power_on_nodes.return_value = {}

        with TwistedLoggerFixture() as twisted_log:
            Node.objects.start_nodes(
                list(node.system_id for node in nodes), user)

        self.assertThat(power_on_nodes, MockCalledOnceWith(ANY))

        nodes_start_info_observed = power_on_nodes.call_args[0][0]
        nodes_start_info_expected = [
            (node.system_id, node.hostname, node.nodegroup.uuid, power_info)
            for node, power_info in izip(nodes, power_infos)
        ]

        # If the following fails the diff is big, but it's useful.
        self.maxDiff = None

        self.assertItemsEqual(
            nodes_start_info_expected,
            nodes_start_info_observed)

        # No complaints are made to the Twisted log.
        self.assertEqual("", twisted_log.dump())

    def test__logs_errors_to_twisted_log_when_starting_nodes(self):
        power_on_nodes = self.patch(node_module, "power_on_nodes")
        power_on_nodes.return_value = {
            factory.make_name("system_id"): defer.fail(
                ZeroDivisionError("Defiance is futile"))
        }

        with TwistedLoggerFixture() as twisted_log:
            Node.objects.start_nodes([], factory.make_user())

        # Complaints go only to the Twisted log.
        self.assertDocTestMatches(
            """\
            Unhandled Error
            Traceback (...
            Failure: exceptions.ZeroDivisionError: Defiance is futile
            """,
            twisted_log.dump())

    def test__ignores_nodes_without_mac(self):
        user = factory.make_user()
        node = factory.make_node(status=NODE_STATUS.ALLOCATED, owner=user)
        nodes_started = Node.objects.start_nodes([node.system_id], user)
        self.assertItemsEqual([], nodes_started)

    def test__marks_allocated_node_as_deploying(self):
        user = factory.make_user()
        [node] = self.make_acquired_nodes_with_macs(user, count=1)
        nodes_started = Node.objects.start_nodes([node.system_id], user)
        self.assertItemsEqual([node], nodes_started)
        self.assertEqual(
            NODE_STATUS.DEPLOYING, reload_object(node).status)

    def test__does_not_change_state_of_deployed_node(self):
        user = factory.make_user()
        node = factory.make_node(
            power_type='ether_wake', status=NODE_STATUS.DEPLOYED,
            owner=user)
        factory.make_mac_address(node=node)
        nodes_started = Node.objects.start_nodes([node.system_id], user)
        self.assertItemsEqual([node], nodes_started)
        self.assertEqual(
            NODE_STATUS.DEPLOYED, reload_object(node).status)


class NodeStaticIPClaimingTest(MAASServerTestCase):

    def test_claim_static_ips_ignores_unmanaged_macs(self):
        node = factory.make_node()
        factory.make_mac_address(node=node)
        observed = node.claim_static_ips()
        self.assertItemsEqual([], observed)

    def test_claim_static_ips_creates_task_for_each_managed_mac(self):
        nodegroup = factory.make_node_group()
        node = factory.make_node(nodegroup=nodegroup)

        # Add some MACs attached to managed interfaces.
        number_of_macs = 2
        for _ in range(0, number_of_macs):
            ngi = factory.make_node_group_interface(
                nodegroup,
                management=NODEGROUPINTERFACE_MANAGEMENT.DHCP)
            factory.make_mac_address(node=node, cluster_interface=ngi)

        observed = node.claim_static_ips()
        expected = [
            'provisioningserver.tasks.add_new_dhcp_host_map'] * number_of_macs

        self.assertEqual(
            expected,
            [task.task for task in observed]
            )

    def test_claim_static_ips_creates_deletion_task(self):
        # If dhcp leases exist before creating a static IP, the code
        # should attempt to remove their host maps.
        node = factory.make_node_with_mac_attached_to_nodegroupinterface()
        factory.make_dhcp_lease(
            nodegroup=node.nodegroup, mac=node.get_primary_mac().mac_address)

        observed = node.claim_static_ips()

        self.assertEqual(
            [
                'celery.chain',
                'provisioningserver.tasks.add_new_dhcp_host_map',
            ],
            [
                task.task for task in observed
            ])

        # Probe the chain to make sure it has the deletion task.
        self.assertEqual(
            'provisioningserver.tasks.remove_dhcp_host_map',
            observed[0].tasks[0].task,
            )

    def test_claim_static_ips_ignores_interface_with_no_static_range(self):
        node = factory.make_node_with_mac_attached_to_nodegroupinterface()
        ngi = node.get_primary_mac().cluster_interface
        ngi.static_ip_range_low = None
        ngi.static_ip_range_high = None
        ngi.save()

        observed = node.claim_static_ips()
        self.assertItemsEqual([], observed)

    def test_claim_static_ips_deallocates_if_cant_complete_all_macs(self):
        node = factory.make_node_with_mac_attached_to_nodegroupinterface()
        self.patch(
            MACAddress,
            'claim_static_ips').side_effect = StaticIPAddressExhaustion
        deallocate_call = self.patch(
            StaticIPAddressManager, 'deallocate_by_node')
        self.assertRaises(StaticIPAddressExhaustion, node.claim_static_ips)
        self.assertThat(deallocate_call, MockCalledOnceWith(node))

    def test_claim_static_ips_does_not_deallocate_if_completes_all_macs(self):
        node = factory.make_node_with_mac_attached_to_nodegroupinterface()
        deallocate_call = self.patch(
            StaticIPAddressManager, 'deallocate_by_node')
        node.claim_static_ips()

        self.assertThat(deallocate_call, MockNotCalled())

    def test_claim_static_ips_updates_dns(self):
        node = factory.make_node_with_mac_attached_to_nodegroupinterface(
            management=NODEGROUPINTERFACE_MANAGEMENT.DHCP_AND_DNS)
        node.nodegroup.status = NODEGROUP_STATUS.ACCEPTED
        change_dns_zones = self.patch(dns_config, 'change_dns_zones')
        node.claim_static_ips()
        self.assertThat(change_dns_zones, MockCalledOnceWith([node.nodegroup]))

    def test_claim_static_ips_creates_no_tasks_if_existing_IP(self):
        node = factory.make_node_with_mac_attached_to_nodegroupinterface()
        primary_mac = node.get_primary_mac()
        primary_mac.claim_static_ips(alloc_type=IPADDRESS_TYPE.STICKY)
        ngi = factory.make_node_group_interface(
            node.nodegroup, management=NODEGROUPINTERFACE_MANAGEMENT.DHCP)
        second_mac = factory.make_mac_address(node=node, cluster_interface=ngi)
        observed_tasks = node.claim_static_ips()

        # We expect only a single task for the one MAC which did not
        # already have any IP.
        expected = ['provisioningserver.tasks.add_new_dhcp_host_map']
        self.assertEqual(
            expected,
            [task.task for task in observed_tasks]
            )
        task_mapping_arg = observed_tasks[0].kwargs["mappings"]
        [observed_mac] = task_mapping_arg.values()
        self.assertEqual(second_mac.mac_address.get_raw(), observed_mac)


class TestClaimStaticIPAddresses(MAASTestCase):
    """Tests for `Node.claim_static_ip_addresses`."""

    def test__returns_empty_list_if_no_iface(self):
        node = factory.make_node()
        self.assertEqual([], node.claim_static_ip_addresses())

    def test__returns_empty_list_if_no_iface_on_managed_network(self):
        node = factory.make_node()
        factory.make_mac_address(node=node)
        self.assertEqual([], node.claim_static_ip_addresses())

    def test__returns_mapping_for_iface_on_managed_network(self):
        node = factory.make_node_with_mac_attached_to_nodegroupinterface()
        static_mappings = node.claim_static_ip_addresses()
        [static_ip] = node.static_ip_addresses()
        [mac_address] = node.macaddress_set.all()
        self.assertEqual(
            [(static_ip, unicode(mac_address))],
            static_mappings)

    def test__returns_mappings_for_ifaces_on_managed_network(self):
        node = factory.make_node_with_mac_attached_to_nodegroupinterface()
        mac_address2 = factory.make_mac_address(node=node)
        [managed_interface] = node.nodegroup.get_managed_interfaces()
        mac_address2.cluster_interface = managed_interface
        mac_address2.save()
        static_mappings = node.claim_static_ip_addresses()
        self.assertItemsEqual(
            [(mac_address.ip_addresses.first().ip, unicode(mac_address))
             for mac_address in node.macaddress_set.all()],
            static_mappings)

    def test__ignores_mac_addresses_with_non_auto_addresses(self):
        node = factory.make_node_with_mac_attached_to_nodegroupinterface()
        mac_address1 = node.macaddress_set.first()
        mac_address2 = factory.make_mac_address(node=node)
        [managed_interface] = node.nodegroup.get_managed_interfaces()
        mac_address2.cluster_interface = managed_interface
        mac_address2.claim_static_ips(IPADDRESS_TYPE.STICKY)
        mac_address2.save()

        self.assertRaises(
            StaticIPAddressTypeClash, mac_address2.claim_static_ips)

        static_mappings = node.claim_static_ip_addresses()
        self.assertItemsEqual(
            [(mac_address1.ip_addresses.first().ip, unicode(mac_address1))],
            static_mappings)

    def test__allocates_all_addresses_or_none_at_all(self):
        node = factory.make_node()
        nodegroupinterface = factory.make_node_group_interface(
            node.nodegroup, management=NODEGROUPINTERFACE_MANAGEMENT.DHCP)
        mac_address1 = factory.make_mac_address(node=node)
        mac_address2 = factory.make_mac_address(node=node)
        mac_address1.cluster_interface = nodegroupinterface
        mac_address2.cluster_interface = nodegroupinterface
        mac_address1.save()
        mac_address2.save()

        # Narrow the static range to only one address.
        nodegroupinterface.static_ip_range_high = (
            nodegroupinterface.static_ip_range_low)
        nodegroupinterface.save()

        self.assertRaises(
            StaticIPAddressExhaustion, node.claim_static_ip_addresses)

        count_mac_ips = lambda mac: mac.ip_addresses.count()
        has_no_ip_addresses = AfterPreprocessing(count_mac_ips, Equals(0))

        self.expectThat(mac_address1, has_no_ip_addresses)
        self.expectThat(mac_address2, has_no_ip_addresses)


class TestDeploymentStatus(MAASServerTestCase):
    """Tests for node.get_deployment_status."""

    def test_returns_deployed_when_deployed(self):
        node = factory.make_node(owner=factory.make_user(), netboot=False)
        self.assertEqual("Deployed", node.get_deployment_status())

    def test_returns_deploying_when_deploying(self):
        node = factory.make_node(owner=factory.make_user(), netboot=True)
        self.assertEqual("Deploying", node.get_deployment_status())

    def test_returns_broken_when_broken(self):
        node = factory.make_node(status=NODE_STATUS.BROKEN)
        self.assertEqual("Broken", node.get_deployment_status())

    def test_returns_unused_when_unused(self):
        node = factory.make_node(owner=None)
        self.assertEqual("Unused", node.get_deployment_status())
