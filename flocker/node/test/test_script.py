# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
Tests for :module:`flocker.node.script`.
"""
import netifaces
import yaml

from jsonschema.exceptions import ValidationError

from zope.interface.verify import verifyObject

from twisted.internet.defer import Deferred
from twisted.python.filepath import FilePath
from twisted.trial.unittest import SynchronousTestCase
from twisted.application.service import Service
from twisted.internet.ssl import ClientContextFactory

from ...volume.testtools import make_volume_options_tests
from ...common.script import ICommandLineScript

from ..script import (
    AgentScript, ContainerAgentOptions,
    AgentServiceFactory, DatasetAgentOptions, validate_configuration,
    _context_factory_and_credential, NewAgentScript,
)

from .._loop import AgentLoopService
from .._deploy import P2PManifestationDeployer
from ...testtools import MemoryCoreReactor, random_name
from ...ca.testtools import get_credential_sets


def setup_config(test):
    """
    Create a configuration file and certificates for a dataset agent in a
    temporary directory.

    Sets ``config`` attribute on the test instance with the path to the
    config file.

    :param test: A ``TestCase`` instance.
    """
    ca_set, _ = get_credential_sets()
    scratch_directory = FilePath(test.mktemp())
    scratch_directory.makedirs()
    test.config = scratch_directory.child('dataset-config.yml')
    test.config.setContent(
        yaml.safe_dump({
            u"control-service": {
                u"hostname": u"10.0.0.1",
                u"port": 1234,
            },
            u"dataset": {
                u"backend": u"zfs",
                u"name": random_name(test),
                # XXX Can't put FilePath in a yaml file (well you can but
                # .... :/)
                u"mount_root": scratch_directory.child(b"mount_root").path,
                u"volume_config_path": scratch_directory.child(
                    b"volume_config.json"
                ).path,
            },
            u"version": 1,
        }))
    ca_set.copy_to(scratch_directory, node=True)
    test.ca_set = ca_set
    test.non_existent_file = scratch_directory.child('missing-config.yml')

deployer = object()


# This should have an explicit interface:
# https://clusterhq.atlassian.net/browse/FLOC-1929
def deployer_factory_stub(**kw):
    if set(kw.keys()) != {"node_uuid", "cluster_uuid", "hostname"}:
        raise TypeError("wrong arguments")
    return deployer


class DummyAgentService:
    def __init__(self, loop_service, reactor, configuration):
        self.loop_service = loop_service

    @classmethod
    def for_loop_service(cls, loop_service):
        return lambda *a, **kw: cls(loop_service, *a, **kw)

    def get_api(self):
        return None

    def get_deployer(self, api):
        return None

    def get_loop_service(self, deployer):
        return self.loop_service


class ZFSGenericAgentScriptTests(SynchronousTestCase):
    """
    Tests for ``NewAgentScript`` using ZFS configuration.
    """
    def setUp(self):
        setup_config(self)

    def test_no_immediate_stop(self):
        """
        The ``Deferred`` returned from ``NewAgentScript`` is not fired.
        """
        script = NewAgentScript()
        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.config.path])
        self.assertNoResult(script.main(MemoryCoreReactor(), options))

    def test_starts_convergence_loop(self):
        """
        ``NewAgentScript.main`` starts a convergence loop service.
        """

        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.config.path])

        loop_service = Service()
        agent_service_factory = DummyAgentService.for_loop_service(
            loop_service
        )
        script = NewAgentScript(agent_service_factory=agent_service_factory)
        script.main(MemoryCoreReactor(), options)

        self.assertTrue(loop_service.running)

    def test_config_validated(self):
        """
        ``NewAgentScript.main`` validates the configuration file.
        """
        self.config.setContent(b"INVALID")

        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.config.path])
        test_reactor = MemoryCoreReactor()

        self.assertRaises(
            ValidationError,
            NewAgentScript().main, test_reactor, options,
        )

    def test_missing_configuration_file(self):
        """
        ``NewAgentScript.main`` raises an ``IOError`` if the given
        configuration file does not exist.
        """
        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.non_existent_file.path])
        test_reactor = MemoryCoreReactor()

        self.assertRaises(
            IOError,
            NewAgentScript().main, test_reactor, options,
        )


# AgentService tests :
#
# get_loop returns an AgentLoopService with the right reactor, host, port,
# deployer, context factory
#
# get_deployer returns a P2PManifestationDeployer for zfs config, a
# BlockDeviceDeployer for loopback config
#
# get_deployer passes the node_uuid taken from the node_credentials in to the
# deployer_factory
#
# AgentService has a default port of 4524 I guess
#
# in the zfs case, get_api returns a started VolumeService


def get_all_ips():
    """
    Find all IPs for this machine.

    :return: ``list`` of IP addresses (``bytes``).
    """
    ips = []
    interfaces = netifaces.interfaces()
    for interface in interfaces:
        addresses = netifaces.ifaddresses(interface)
        ipv4 = addresses.get(netifaces.AF_INET)
        if not ipv4:
            continue
        for address in ipv4:
            ips.append(address['addr'])
    return ips


class AgentServiceFactoryTests(SynchronousTestCase):
    """
    Tests for ``AgentServiceFactory``.
    """
    def setUp(self):
        setup_config(self)

    def test_uuids_from_certificate(self):
        """
        The created deployer got its node UUID and cluster UUID from the given
        node certificate.
        """
        result = []

        def factory(hostname, node_uuid, cluster_uuid):
            result.append((node_uuid, cluster_uuid))
            return object()

        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.config.path])
        service_factory = AgentServiceFactory(deployer_factory=factory)
        service_factory.get_service(MemoryCoreReactor(), options)
        self.assertEqual(
            (self.ca_set.node.uuid,
             self.ca_set.node.cluster_uuid),
            result[0])

    def test_get_service(self):
        """
        ``AgentServiceFactory.get_service`` creates an ``AgentLoopService``
        configured with the destination given in the config file given by the
        options.
        """
        reactor = MemoryCoreReactor()
        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.config.path])
        service_factory = AgentServiceFactory(
            deployer_factory=deployer_factory_stub,
        )
        self.assertEqual(
            AgentLoopService(
                reactor=reactor,
                deployer=deployer,
                host=b"10.0.0.1",
                port=1234,
                context_factory=_context_factory_and_credential(
                    self.config.parent(), b"10.0.0.1", 1234).context_factory,
            ),
            service_factory.get_service(reactor, options)
        )

    def test_default_port(self):
        """
        ``AgentServiceFactory.get_service`` creates an ``AgentLoopService``
        configured with port 4524 if no port is specified.
        """
        self.config.setContent(
            yaml.safe_dump({
                u"control-service": {
                    u"hostname": u"10.0.0.2",
                },
                u"dataset": {
                    u"backend": u"zfs",
                },
                u"version": 1,
            }))

        reactor = MemoryCoreReactor()
        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.config.path])
        service_factory = AgentServiceFactory(
            deployer_factory=deployer_factory_stub,
        )
        self.assertEqual(
            AgentLoopService(
                reactor=reactor,
                deployer=deployer,
                host=b"10.0.0.2",
                port=4524,
                context_factory=_context_factory_and_credential(
                    self.config.parent(), b"10.0.0.2", 4524).context_factory,
            ),
            service_factory.get_service(reactor, options)
        )

    def test_config_validated(self):
        """
        ``AgentServiceFactory.get_service`` validates the configuration file.
        """
        self.config.setContent("INVALID")
        reactor = MemoryCoreReactor()
        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.config.path])
        service_factory = AgentServiceFactory(
            deployer_factory=deployer_factory_stub,
        )

        self.assertRaises(
            ValidationError,
            service_factory.get_service, reactor, options,
        )

    def test_deployer_factory_called_with_ip(self):
        """
        ``AgentServiceFactory.main`` calls its ``deployer_factory`` with one
        of the node's IPs.
        """
        spied = []

        def deployer_factory(node_uuid, hostname, cluster_uuid):
            spied.append(hostname)
            return object()

        reactor = MemoryCoreReactor()
        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.config.path])
        agent = AgentServiceFactory(deployer_factory=deployer_factory)
        agent.get_service(reactor, options)
        self.assertIn(spied[0], get_all_ips())

    def test_missing_configuration_file(self):
        """
        ``AgentServiceFactory.get_service`` raises an ``IOError`` if the given
        configuration file does not exist.
        """
        reactor = MemoryCoreReactor()
        options = DatasetAgentOptions()
        options.parseOptions([b"--agent-config", self.non_existent_file.path])
        service_factory = AgentServiceFactory(
            deployer_factory=deployer_factory_stub,
        )

        self.assertRaises(
            IOError,
            service_factory.get_service, reactor, options,
        )


class AgentScriptTests(SynchronousTestCase):
    """
    Tests for ``AgentScript``.
    """
    def setUp(self):
        self.reactor = MemoryCoreReactor()
        self.options = DatasetAgentOptions()

    def test_interface(self):
        """
        ``AgentScript`` instances provide ``ICommandLineScript``.
        """
        self.assertTrue(
            verifyObject(
                ICommandLineScript,
                AgentScript(
                    service_factory=lambda reactor, options: Service()
                )
            )
        )

    def test_service_factory_called_with_main_arguments(self):
        """
        ``AgentScript`` calls the ``service_factory`` with the reactor
        and options passed to ``AgentScript.main``.
        """
        args = []
        service = Service()

        def service_factory(reactor, options):
            args.append((reactor, options))
            return service

        agent = AgentScript(service_factory=service_factory)
        agent.main(self.reactor, self.options)
        self.assertEqual([(self.reactor, self.options)], args)

    def test_main_starts_service(self):
        """
        ```AgentScript.main`` starts the service created by its
        ``service_factory`` .
        """
        service = Service()
        agent = AgentScript(
            service_factory=lambda reactor, options: service
        )
        agent.main(self.reactor, self.options)
        self.assertTrue(service.running)

    def test_main_stops_service(self):
        """
        When the reactor passed to ``AgentScript.main`` shuts down, the
        service created by the ``service_factory`` is stopped.
        """
        service = Service()
        agent = AgentScript(
            service_factory=lambda reactor, options: service
        )
        agent.main(self.reactor, self.options)
        self.reactor.fireSystemEvent("shutdown")
        self.assertFalse(service.running)

    def test_main_deferred_fires_after_service_stop(self):
        """
        The ``Deferred`` returned by ``AgentScript.main`` doesn't fire
        until after the ``Deferred`` returned by the ``stopService`` method of
        the service created by ``service_factory``.
        """
        shutdown_deferred = Deferred()

        class SlowShutdown(Service):
            def stopService(self):
                return shutdown_deferred

        service = SlowShutdown()
        agent = AgentScript(
            service_factory=lambda reactor, options: service
        )
        stop_deferred = agent.main(self.reactor, self.options)
        self.reactor.fireSystemEvent("shutdown")
        self.assertNoResult(stop_deferred)
        shutdown_deferred.callback(None)
        self.assertIs(None, self.successResultOf(stop_deferred))


def make_amp_agent_options_tests(options_type):
    """
    Create a test case which contains the tests that should apply to any and
    all convergence agents (dataset or container).

    :param options_type: An ``Options`` subclass  to be tested.

    :return: A ``SynchronousTestCase`` subclass defining tests for that options
        type.
    """

    class Tests(SynchronousTestCase):
        def setUp(self):
            self.options = options_type()
            self.scratch_directory = FilePath(self.mktemp())
            self.scratch_directory.makedirs()
            self.sample_content = yaml.safe_dump({
                u"control-service": {
                    u"hostname": u"10.0.0.1",
                    u"port": 4524,
                },
                u"version": 1,
            })
            self.config = self.scratch_directory.child('dataset-config.yml')
            self.config.setContent(self.sample_content)

        def test_default_config_file(self):
            """
            The default config file is a FilePath with path
            ``/etc/flocker/agent.yml``.
            """
            self.options.parseOptions([])
            self.assertEqual(
                self.options["agent-config"],
                FilePath("/etc/flocker/agent.yml"))

        def test_custom_config_file(self):
            """
            The ``--config-file`` command-line option allows configuring
            the config file.
            """
            self.options.parseOptions(
                [b"--agent-config", b"/etc/foo.yml"])
            self.assertEqual(
                self.options["agent-config"],
                FilePath("/etc/foo.yml"))

    return Tests


class ValidateConfigurationTests(SynchronousTestCase):
    """
    Tests for :func:`validate_configuration`.
    """

    def setUp(self):
        # This is a sample working configuration which tests can modify.
        self.configuration = {
            u"control-service": {
                u"hostname": u"10.0.0.1",
                u"port": 1234,
            },
            u"dataset": {
                u"backend": u"zfs",
                u"pool": u"custom-pool",
            },
            "version": 1,
        }

    def test_valid_zfs_configuration(self):
        """
        No exception is raised when validating a valid configuration with a
        ZFS backend.
        """
        # Nothing is raised
        validate_configuration(self.configuration)

    def test_valid_loopback_configuration(self):
        """
        No exception is raised when validating a valid configuration with a
        loopback backend.
        """
        self.configuration['dataset'] = {
            u"backend": u"loopback",
            u"pool": u"custom-pool",
        }
        # Nothing is raised
        validate_configuration(self.configuration)

    def test_port_optional(self):
        """
        The control service agent's port is optional.
        """
        self.configuration['control-service'].pop('port')
        # Nothing is raised
        validate_configuration(self.configuration)

    def test_zfs_pool_optional(self):
        """
        No exception is raised when validating a ZFS backend is specified but
        a ZFS pool is not.
        """
        self.configuration['dataset'] = {
            u"backend": u"zfs",
        }
        # Nothing is raised
        validate_configuration(self.configuration)

    def test_loopback_pool_optional(self):
        """
        No exception is raised when validating a loopback backend is specified
        but a loopback pool is not.
        """
        self.configuration['dataset'] = {
            u"backend": u"loopback",
        }
        # Nothing is raised
        validate_configuration(self.configuration)

    def test_error_on_invalid_configuration_type(self):
        """
        A ``ValidationError`` is raised if the config file is not formatted
        as a dictionary.
        """
        self.configuration = "INVALID"
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_invalid_hostname(self):
        """
        A ``ValidationError`` is raised if the given control service
        hostname is not a valid hostname.
        """
        self.configuration['control-service']['hostname'] = u"-1"
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_missing_control_service(self):
        """
        A ``ValidationError`` is raised if the config file does not
        contain a ``u"control-service"`` key.
        """
        self.configuration.pop('control-service')
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_missing_hostname(self):
        """
        A ``ValidationError`` is raised if the config file does not
        contain a hostname in the ``u"control-service"`` key.
        """
        self.configuration['control-service'].pop('hostname')
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_missing_version(self):
        """
        A ``ValidationError`` is raised if the config file does not contain
        a ``u"version"`` key.
        """
        self.configuration.pop('version')
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_high_version(self):
        """
        A ``ValidationError`` is raised if the version specified is greater
        than 1.
        """
        self.configuration['version'] = 2
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_low_version(self):
        """
        A ``ValidationError`` is raised if the version specified is lower
        than 1.
        """
        self.configuration['version'] = 0
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_invalid_port(self):
        """
        The control service agent's port must be an integer.
        """
        self.configuration['control-service']['port'] = 1.1
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_missing_dataset(self):
        """
        A ``ValidationError`` is raised if the config file does not contain
        a ``u"dataset"`` key.
        """
        self.configuration.pop('dataset')
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_missing_dataset_backend(self):
        """
        The dataset key must contain a backend type.
        """
        self.configuration['dataset'] = {}
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)

    def test_error_on_invalid_dataset_type(self):
        """
        The dataset key must contain a valid dataset type.
        """
        self.configuration['dataset'] = {"backend": "invalid"}
        self.assertRaises(
            ValidationError, validate_configuration, self.configuration)


class DatasetAgentOptionsTests(
        make_amp_agent_options_tests(DatasetAgentOptions)
):
    """
    Tests for ``DatasetAgentOptions``.
    """


class ContainerAgentOptionsTests(
        make_amp_agent_options_tests(ContainerAgentOptions)
):
    """
    Tests for ``ContainerAgentOptions``.
    """


class DatasetAgentVolumeTests(make_volume_options_tests(
        DatasetAgentOptions, [])):
    """
    Tests for the volume configuration arguments of ``DatasetAgentOptions``.

    XXX This should be removed as part of FLOC-1924.
    """
