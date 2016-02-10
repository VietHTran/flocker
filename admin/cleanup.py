# Copyright ClusterHQ Inc.  See LICENSE file for details.
"""
Utilities for cloud resource cleanup.
"""
from datetime import datetime, timedelta
import json
import sys
from uuid import UUID
import yaml
from yaml.parser import ParserError

from characteristic import attributes

from dateutil.parser import parse as parse_date
from dateutil.tz import tzutc

from libcloud.compute.base import NodeState, StorageVolume
from libcloud.compute.providers import get_driver, Provider

from twisted.python.filepath import FilePath
from twisted.python.log import err
from twisted.python.usage import Options, UsageError

# Marker value defined by ``flocker.testtools.cluster_utils.MARKER``.  This
# should never change and should always identify test-created clusters.
from flocker.testtools.cluster_utils import MARKER


DEFAULT_INSTANCE_NAME_PREFIXES = (
    'acceptance-test-',
    'client-test-'
)


def get_creation_time(node):
    """
    Get the creation time of a libcloud node.

    Rackspace and EC2 store the information in different metadeta.

    :return: The creation time, if available.
    :rtype: datetime or None
    """
    date_string = node.extra.get("created", node.extra.get("launch_time"))
    if date_string is None:
        return None
    else:
        return parse_date(date_string)


def get_rackspace_driver(rackspace):
    """
    Get a libcloud Rackspace driver given some credentials and other
    configuration.
    """
    rackspace = get_driver(Provider.RACKSPACE)(
        rackspace['username'], rackspace['key'],
        region=rackspace['region'],
    )
    return rackspace


def get_ec2_driver(aws):
    """
    Get a libcloud EC2 driver given some credentials and other configuration.
    """
    ec2 = get_driver(Provider.EC2)(
        aws['access_key'], aws['secret_access_token'],
        region=aws['region'],
    )
    return ec2


def _get_volume_creation_time(volume):
    """
    Extract the creation time from an AWS or Rackspace volume.

    XXX: libcloud doesn't represent volume creation time uniformly across
    drivers.  Thus this method only works on drivers specifically accounted
    for. Should be extended or refactored for GCE support.

    :param libcloud.compute.base.StorageVolume volume: The volume to query.
    :returns: The datetime when the ``volume`` was created.
    """
    try:
        # AWS
        return volume.extra['create_time']
    except KeyError:
        # Rackspace.  Timestamps have no timezone indicated.  Manual
        # experimentation indicates timestamps are in UTC (which of course
        # is the only reasonable thing).
        return parse_date(
            volume.extra['created_at']
        ).replace(tzinfo=tzutc())


def _get_volume_region(volume):
    """
    Extract the region name from an AWS or Rackspace volume.

    XXX: libcloud doesn't represent volume creation time uniformly across
    drivers.  Thus this method only works on drivers specifically accounted
    for. Should be extended or refactored for GCE support.

    :param libcloud.compute.base.StorageVolume volume: The volume to query.
    :returns: The region of the ``volume``
    """
    return (
        # Rackspace
        getattr(volume.driver, "region", None) or
        # AWS
        getattr(volume.driver, "region_name", None)
    )


def _describe_volume(volume):
    """
    Create a dictionary giving lots of interesting details about a cloud
    volume.

    :param libcloud.compute.base.StorageVolume volume: The volume to query.
    :returns: A JSON serializable dict of ``volume`` information.
    """
    return {
        'id': volume.id,
        'creation_time': _format_time(
            _get_volume_creation_time(volume),
        ),
        'provider': volume.driver.name,
        'region': _get_volume_region(volume),
        # *Stuffed* with non-JSON-encodable goodies.
        'extra': repr(volume.extra),
    }


@attributes(["lag", "marker"])
class CleanVolumes(object):
    """
    Destroy volumes that leaked into the cloud from the acceptance and
    functional test suites.
    """
    def start(self, config, dry_run=False):
        """
        Clean up old volumes belonging to test-created Flocker clusters.
        """
        drivers = self._get_cloud_drivers(config)
        volumes = self._get_cloud_volumes(drivers)
        actions = self._filter_test_volumes(self.lag, volumes)
        return actions

    def _get_cloud_drivers(self, config):
        """
        From the private buildbot configuration, construct a list of all of the
        libcloud drivers where leaked volumes might be found.
        """
        base_ec2 = config["aws"]

        drivers = [
            get_rackspace_driver(config["rackspace"]),
            get_ec2_driver(base_ec2),
        ]

        for extra in config.get("extra-aws", []):
            extra_driver_config = base_ec2.copy()
            extra_driver_config.update(extra)
            drivers.append(get_ec2_driver(extra_driver_config))
        return drivers

    def _get_cloud_volumes(self, drivers):
        """
        From the given libcloud drivers, look up all existing volumes.
        """
        volumes = []
        for driver in drivers:
            volumes.extend(driver.list_volumes())
        return volumes

    def _get_cluster_id(self, volume):
        """
        Extract the Flocker-specific cluster identifier from the given volume.

        :param libcloud.compute.base.StorageVolume volume: The volume to query.
        :raise: ``KeyError`` if the given volume is not tagged with a Flocker
            cluster id.
        """
        return _get_tag(volume, 'flocker-cluster-id')

    def _is_test_cluster(self, cluster_id):
        """
        Determine whether or not the given Flocker cluster identifier belongs
        to a cluster created by a test suite run.

        :return: ``True`` if it does, ``False`` if it does not.
        """
        try:
            return UUID(cluster_id).node == self.marker
        except:
            err(None, "Could not parse cluster_id {!r}".format(cluster_id))
            return False

    def _is_test_volume(self, volume):
        """
        Determine whether or not the given volume belongs to a test-created
        Flocker cluster (and is therefore subject to automatic destruction).

        :return: ``True`` if it does, ``False`` if it does not.
        """
        try:
            cluster_id = self._get_cluster_id(volume)
        except KeyError:
            return False
        return self._is_test_cluster(cluster_id)

    def _filter_test_volumes(self, maximum_age, volumes):
        """
        From the given list of volumes, find volumes created by tests which are
        older than the maximum age.

        :param timedelta maximum_age: The oldest a volume may be without being
            considered old enough to include in the result.
        :param list all_volumes: The libcloud volumes representing all the
            volumes we can see on a particular cloud service.

        :rtype: ``VolumeActions``
        """
        now = datetime.now(tz=tzutc())
        destroy = []
        keep = []
        for volume in volumes:
            created = _get_volume_creation_time(volume)
            if self._is_test_volume(volume) and now - created > maximum_age:
                destroy.append(volume)
            else:
                keep.append(volume)
        return VolumeActions(destroy=destroy, keep=keep)


@attributes(['lag', 'prefixes'])
class CleanAcceptanceInstances(object):
    """
    :ivar timedelta lag: The age of instances to destroy.
    :param prefixes: List of prefixes of instances to destroy.
    """
    def start(self, config, dry_run=False):
        # Get the libcloud drivers corresponding to the acceptance tests.
        rackspace = get_rackspace_driver(config["rackspace"])
        ec2 = get_ec2_driver(config["aws"])
        drivers = [rackspace, ec2]

        # Get the prefixes of the instance names, appending the creator from
        # the config.
        creator = config['metadata']['creator']
        prefixes = tuple(map(lambda prefix: prefix + creator, self.prefixes))

        # Find out the cutoff time to use.
        now = datetime.now(tzutc())
        cutoff = now - self.lag

        # Get all the nodes from the cloud providers
        all_nodes = []
        for driver in drivers:
            all_nodes += driver.list_nodes()

        # Filter them for running nodes with the right prefix.
        test_nodes = [
            node for node
            in all_nodes
            if node.name.startswith(prefixes)
            # Also, terminated nodes that still show up.  An OpenStack bug
            # causes these to hang around sometimes.  They're not billed in
            # this state but they do count towards RAM quotas.  Quoth Rackspace
            # support:
            #
            # > The complete fix for this issue is expected in the next
            # > Openstack iteration (mid August).  Until then what can be done
            # > is just to issue another delete against the same instance.  The
            # > servers are only billed when they are in Active (green) status,
            # > so the deleted instances are not billed.
            #
            # So consider any nodes in that state as potential destruction
            # targets.
            and node.state in (NodeState.RUNNING, NodeState.TERMINATED)
        ]

        # Split the nodes into kept and destroyed nodes;
        # destroying the ones older than the cut-off.
        destroyed_nodes = []
        kept_nodes = []
        for node in test_nodes:
            creation_time = get_creation_time(node)
            if creation_time is not None and creation_time < cutoff:
                destroyed_nodes.append(node)
            else:
                kept_nodes.append(node)

        return {
            'destroyed': destroyed_nodes,
            'kept': kept_nodes,
        }

    def log(self, result):
        """
        Log the nodes kept and destroyed.
        """
        for kind, nodes in result.iteritems():
            content = _dumps([
                {
                    'id': node.id,
                    'name': node.name,
                    'provider': node.driver.name,
                    'creation_time': _format_time(get_creation_time(node)),
                }
                for node in nodes
            ])
            self.addCompleteLog(name=kind, text=content)
        if len(result['destroyed']) > 0:
            # We fail if we destroyed any nodes, because that means that
            # something is leaking nodes.
            self.finished(FAILURE)
        else:
            self.finished(SUCCESS)


class _ActionEncoder(json.JSONEncoder):
    """
    JSON encoder that can encode ValueConstant etc.
    """
    def default(self, obj):
        if isinstance(obj, VolumeActions):
            return dict(
                keep=obj.keep,
                destroy=obj.destroy
            )
        if isinstance(obj, StorageVolume):
            return _describe_volume(obj)
        return json.JSONEncoder.default(self, obj)


def _dumps(obj):
    """
    JSON encode an object using some visually pleasing formatting.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        indent=4,
        separators=(',', ': '),
        cls=_ActionEncoder
    )


def _format_time(when):
    """
    Format a time to ISO8601 format.  Or if there is no time, just return
    ``None``.
    """
    if when is None:
        return None
    return datetime.isoformat(when)


def _get_tag(volume, tag_name):
    """
    Get a "tag" from an EBS or Rackspace volume.

    libcloud doesn't represent tags uniformly across drivers.  Thus this method
    only works on drivers specifically account for.

    :raise: ``KeyError`` if the tag is not present.
    """
    return volume.extra.get("tags", volume.extra.get("metadata"))[tag_name]


@attributes(["destroy", "keep"])
class VolumeActions(object):
    """
    Represent something to be done to some volumes.

    :ivar destroy: Volumes to destroy.
    :ivar keep: Volumes to keep.
    """


def _existing_file_path_option(option_name, option_value):
    file_path = FilePath(option_value)
    if not file_path.exists():
        raise UsageError(
            u"Problem with --{}. File does not exist: '{}'.".format(
                option_name, file_path.path
            )
        )
    return file_path


def _yaml_configuration_path_option(option_name, option_value):
    yaml_path = _existing_file_path_option(option_name, option_value)
    try:
        configuration = yaml.safe_load(yaml_path.open())
    except ParserError as e:
        raise UsageError(
            u"Problem with --{}. "
            u"Unable to parse YAML from {}. "
            u"Error message: {}.".format(
                option_name, yaml_path.path, unicode(e)
            )
        )
    return configuration


class CleanupCloudResourcesOptions(Options):
    """
    Command line options for ``cleanup_cloud_resources``.
    """
    optFlags = [
        ["dry-run", None,
         "Just print the calculated actions. Don't delete anything."]
    ]

    optParameters = [
        [u"config-file", None, None,
         u"An acceptance.yml file containing cloud credentials.\n",
         lambda option_value: _yaml_configuration_path_option(
             u"config-file", option_value
         )],
        [u"volume-lag", None, timedelta(minutes=30),
         u"The oldest in minutes a volume may be "
         u"without being considered for deletion.\n",
         lambda option_value: timedelta(minutes=int(option_value))],
        [u"instance-lag", None, timedelta(hours=2),
         u"The oldest in minutes an instance may be "
         u"without being considered for deletion.\n",
         lambda option_value: timedelta(minutes=int(option_value))],
        [u"marker", None, MARKER,
         u"The marker which is used "
         u"as the ``node`` in the acceptance test cluster UUID.",
         lambda option_value: int(option_value, base=16)]
    ]

    def opt_instance_name_prefix(self, instance_name_prefix):
        """
        Instances beginning with ``instance_name_prefix`` will be considered
        for deletion. Defaults to: ``DEFAULT_INSTANCE_NAME_PREFIXES``.
        """
        self.setdefault('instance_name_prefixes', []).append(
            instance_name_prefix
        )

    def postOptions(self):
        self["dry-run"] = bool(self["dry-run"])
        if self["config-file"] is None:
            raise UsageError("Missing --config-file option.")
        self.setdefault(
            'instance_name_prefixes',
            list(DEFAULT_INSTANCE_NAME_PREFIXES)
        )


def cleanup_cloud_resources_main(args, base_path, top_level):
    options = CleanupCloudResourcesOptions()

    try:
        options.parseOptions(args)
    except UsageError as e:
        sys.stderr.write(
            u"{}\n"
            u"Usage Error: {}: {}\n".format(
                unicode(options), base_path.basename(), e
            ).encode('utf-8')
        )
        raise SystemExit(1)

    resource_actions = [
        CleanAcceptanceInstances(
            lag=options["instance-lag"],
            prefixes=options["instance_name_prefixes"],
        ).start(
            config=options["config-file"],
            dry_run=options["dry-run"],
        ),
        # CleanVolumes(
        #     lag=options["volume-lag"],
        #     marker=options["marker"],
        # ).start(
        #     config=options["config-file"],
        #     dry_run=options["dry-run"],
        # )
    ]

    print_actions(resource_actions)

    # if not options['dry-run']:
    #     perform_actions(resource_actions)

    do_exit(resource_actions)


def destroy_resource(resource):
    try:
        resource.destroy()
    except:
        err(None, "Destroying resource.")


def perform_actions(resource_actions):
    """
    Unconditionally and irrevocably destroy all of the given cloud volumes.
    """
    for action_group in resource_actions:
        for resource in action_group.destroy:
            destroy_resource(resource)


def print_actions(actions):
    """
    If volumes are destroyed, the operation is considered to have failed.
    The test suite should have cleaned those volumes up.  This is an
    unfortunate time to be reporting the problem but it's better than never
    reporting it.
    """
    sys.stdout.write(
        _dumps(actions).encode('utf-8') + b'\n'
    )


def do_exit(actions):
    for action_group in actions:
        if len(action_group.destroy) > 0:
            # We fail if we destroyed anything because that means that
            # something is not being cleaned up.
            raise SystemExit(1)
