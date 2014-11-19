#    Copyright 2014 Rackspace Inc.
#    All Rights Reserved
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

"""
Defines all the model classes for the various parts of the API.
"""

from datetime import datetime, timedelta
import logging
import time

from ambariclient import base, exceptions, events
from ambariclient.utils import normalize_underscore_case

LOG = logging.getLogger(__name__)


class Bootstrap(base.PollableMixin, base.GeneratedIdentifierMixin, base.QueryableModel):
    """Boostrap hosts by installing the agent on them.

    There are two ways to register hosts with Ambari:
        1. Preload the agent and configuration on the hosts.
        2. Preload an ssh key on the hosts and tell Ambari to bootstrap them.

    Boostrapping involves using an uploaded private SSH key to contact the
    hosts via SSH and tell them to install and configure the Ambari agent.

    It is generally not recommended for production workloads as you're passing
    in the private SSH key to Ambari to contact the hosts, but it's very useful
    for development or proof-of-concept work.

    To bootstrap some hosts:
        ambari.bootstrap.create(hosts=[hostname, hostname, ...],
                                sshKey=ssh_key_as_string,
                                user=username)

    The wait() method works as expected here and will not return until all of
    the bootstrapped hosts are online and registered with the Ambari server.

    The API for boostrap is very inconsistent with the rest of the API.  There
    is no way to query for a collection of bootstrap attempts, for example.  We
    work around that as much as possible, but some things might not work as
    expected.
    """
    path = 'bootstrap'
    primary_key = 'requestId'
    fields = ('status', 'requestId', 'message', 'hostsStatus')

    @events.evented
    def create(self, **kwargs):
        # we always want the verbose response here
        kwargs['verbose'] = True

        if 'ssh_key_path' in kwargs:
            with open(kwargs.pop('ssh_key_path')) as f:
                kwargs['sshKey'] = f.read()

        data = self._generate_input_dict(**kwargs)
        self.load(self.client.post(self.url,
                             content_type="application/json",
                             data=data))
        # this special case morphs the object URL after creation since we don't
        # know the requestId ahead of time
        self._hosts = kwargs['hosts']
        self._href = None
        return self

    @property
    def has_failed(self):
        return True if self.status == 'ERROR' else False

    @property
    def is_finished(self):
        return True if self.status == 'SUCCESS' else False

    @property
    def hosts(self):
        if self._hosts:
            return self.client.hosts(self._hosts)
        return []

    def wait(self, **kwargs):
        """Wait until the bootstrap completes and all hosts are registered.

        Even after bootstrap finishes, there is a slight delay while the agents
        start and register themselves with Ambari.  We add in additional checks
        to ensure they are fully online before returning, because that's all a
        user should care about.
        """
        super(Bootstrap, self).wait(**kwargs);
        # make sure all the hosts are registered as well
        for host in self.hosts:
            host.wait()
        return self

    def inflate(self):
        # the requestId isn't returned in the response body except on create
        # so keep track of it manually.
        requestId = self._data.get('requestId')
        super(Bootstrap, self).inflate()
        self._data['requestId'] = requestId
        return self


class Request(base.PollableMixin, base.GeneratedIdentifierMixin, base.QueryableModel):
    path = 'requests'
    data_key = 'Requests'
    primary_key = 'id'
    fields = ('id', 'request_context', 'status', 'progress_percent',
              'queued_task_count', 'task_count', 'completed_task_count')

    @property
    def has_failed(self):
        return True if self.status == 'FAILED' else False

    @property
    def is_finished(self):
        return True if int(self.progress_percent) == 100 else False

    def create(self, **kwargs):
        data = self._generate_input_dict(**kwargs)
        # we don't have the id for a request, so post to the parent
        # yay for consistency
        self.load(self.client.post(self.parent.url, data=data))
        return self


class Component(base.QueryableModel):
    path = 'components'
    data_key = 'ServiceComponentInfo'
    primary_key = 'component_name'
    fields = ('component_name', 'component_version', 'server_clock',
              'service_name', 'properties')

    def to_json_dict(self):
        """Components are passed in with 'name' as the key rather than the
        primary 'component_name' key for no apparent reason.
        """
        return { 'name': self.component_name }


class HostComponentCollection(base.QueryableModelCollection):
    def install(self):
        """Install all of the components associated with this host."""
        self.load(self.client.put(self.url, data={
            "RequestInfo": {
                "context" :"Install All Components",
                "operation_level": {
                    "level": "HOST",
                    "cluster_name": self.parent.cluster_name,
                    "host_name": self.parent.host_name,
                },
            },
            "Body": {
                "HostRoles": {"state": "INSTALLED"},
            },
        }))
        return self

    def start(self):
        """Start all of the components associated with this host."""
        self.load(self.client.put(self.url, data={
            "RequestInfo": {
                "context" :"Start All Components",
                "operation_level": {
                    "level": "HOST",
                    "cluster_name": self.parent.cluster_name,
                    "host_name": self.parent.host_name,
                },
            },
            "Body": {
                "HostRoles": {"state": "STARTED"},
            },
        }))
        return self

    def stop(self):
        """Stop all of the components associated with this host."""
        self.load(self.client.put(self.url, data={
            "RequestInfo": {
                "context" :"Stop All Components",
                "operation_level": {
                    "level": "HOST",
                    "cluster_name": self.parent.cluster_name,
                    "host_name": self.parent.host_name,
                },
            },
            "Body": {
                "HostRoles": {"state": "INSTALLED"},
            },
        }))
        return self


class HostComponent(Component):
    collection_class = HostComponentCollection
    path = 'host_components'
    data_key = 'HostRoles'
    fields = ('cluster_name', 'component_name', 'desired_stack_id',
              'desired_state', 'host_name', 'maintenance_state', 'service_name',
              'stack_id', 'stale_configs', 'state', 'actual_configs')

    def install(self):
        """Installs this component on the host in question."""
        self.load(self.client.put(self.url, data={
            "RequestInfo": {
                "context": "Install %s" % normalize_underscore_case(self.component_name),
            },
            "HostRoles": {
                "state": "INSTALLED",
            },
        }))
        return self

    def start(self):
        """Starts this component on its host, if already installed."""
        self.load(self.client.put(self.url, data={
            "RequestInfo": {
                "context": "Start %s" % normalize_underscore_case(self.component_name),
            },
            "HostRoles": {
                "state": "STARTED",
            },
        }))
        return self

    def stop(self):
        """Starts this component on its host, if already installed and started."""
        self.load(self.client.put(self.url, data={
            "RequestInfo": {
                "context": "Stop %s" % normalize_underscore_case(self.component_name),
            },
            "HostRoles": {
                "state": "INSTALLED",
            },
        }))
        return self

    def restart(self):
        """Restarts this component on its host, if already installed and started."""
        self.load(self.client.post(self.cluster.requests.url, data={
            "RequestInfo": {
                "command": "RESTART",
                "context": "Restart %s" % normalize_underscore_case(self.component_name),
                "operation_level": {
                    "level": "SERVICE",
                    "cluster_name": self.cluster_name,
                    "service_name": self.service_name,
                },
            },
            "Requests/resource_filters": [{
                "service_name": self.service_name,
                "component_name": self.component_name,
                "hosts": self.host_name,
            }],
        }))
        return self


class Alert(base.DependentModel):
    fields = ("description", "host_name", "last_status", "last_status_time",
              "service_name", "status", "status_time", "output", "actual_status")
    primary_key = None


class Host(base.PollableMixin, base.QueryableModel):
    path = 'hosts'
    data_key = 'Hosts'
    primary_key = 'host_name'
    fields = ('host_name', 'cluster_name', 'cpu_count', 'os_arch', 'disk_info',
              'os_type', 'total_mem', 'host_state', 'ip', 'rack_info',
              'public_host_name', 'host_health_report', 'host_status', 'ip',
              'last_agent_env', 'last_heartbeat_time', 'last_registration_time',
              'maintenance_state', 'os_arch', 'os_type', 'ph_cpu_count',
              'desired_configs')

    default_interval = 5
    default_timeout = 180

    @property
    def has_failed(self):
        """Detect whether the host registration failed.

        There's nothing to do other than wait for it to time out on getting a
        HEALTHY state.
        """
        return False

    @property
    def is_finished(self):
        """Make sure the host is registered with Ambari."""
        return True if self.host_status == 'HEALTHY' else False

    def wait(self, **kwargs):
        # we lose the request checking from base.QueryableModel
        # there might be a more Pythonic way to handle this
        if self.request:
            self.request.wait(**kwargs)

        # the host might give a 404 on the first few attempts, give it a chance
        # to register
        for x in range(1, 7):
            try:
                self.inflate()
            except exceptions.NotFound:
                if x == 6:
                    raise
                else:
                    self._is_inflating = False
                    LOG.debug("Host not found (attempt %d): %s", x, self.host_name)
                    time.sleep(5)

        return super(Host, self).wait(**kwargs)


class HostMaintenance(object):
    def __init__(self, host):
        self.host = host

    def enable(self):
        """Set all components on a host to maintenance mode.

        Maintenance mode disables monitoring, so it's then safe to stop or
        remove components, etc.
        """
        # Ambari API currently has a bug where it doesn't return the Request object here
        self.host.load(self.host.client.put(self.host.url, data={
            "RequestInfo": {
                "context": "Start Maintenance Mode",
                "query": "Hosts/host_name.in(%s)" % self.host.host_name,
            },
            "Body": {
                "Hosts": {"maintenance_state": "ON"},
            },
        }))
        return self.host

    def disable(self):
        """Turn off maintenance mode on this host."""
        # Ambari API currently has a bug where it doesn't return the Request object here
        self.host.load(self.host.client.put(self.host.url, data={
            "RequestInfo": {
                "context": "Stop Maintenance Mode",
                "query": "Hosts/host_name.in(%s)" % self.host.host_name,
            },
            "Body": {
                "Hosts": {"maintenance_state": "OFF"},
            },
        }))
        return self.host


class ClusterHost(Host):
    relationships = {
        'components': HostComponent,
        'alerts': Alert,
    }

    def load(self, response):
        if 'alerts' in response:
            response['alerts'] = response['alerts']['detail']
        return super(ClusterHost, self).load(response)

    def create(self, *args, **kwargs):
        if 'host_group' in kwargs:
            host_group_name = kwargs.pop('host_group')
        if 'blueprint' in kwargs:
            blueprint_name = kwargs.pop('blueprint')
        super(ClusterHost, self).create(*args, **kwargs)
        # the API doesn't have a way to just add a host to a host_group
        # so we handle the logic here to make it easier on the user
        if host_group_name and blueprint_name:
            for host_group in self.client.blueprints(blueprint_name).host_groups:
                if host_group.name == host_group_name:
                    for component in host_group.components:
                        url = self.parent.url + "?Hosts/host_name=%s" % self.host_name
                        self.client.post(url, data={
                            "host_components": [{
                                "HostRoles": {
                                    "component_name": component['name'],
                                },
                            }],
                        })
                    continue

        return self

    @property
    def maintenance(self):
        return HostMaintenance(self)


class TaskAttempt(base.QueryableModel):
    path = 'taskattempts'
    data_key = 'TaskAttempt'
    primary_key = ''
    fields = ()


class Job(base.QueryableModel):
    path = 'jobs'
    data_key = 'Job'
    primary_key = ''
    fields = ()
    relationships = {
        'taskattempts': TaskAttempt,
    }


class Workflow(base.QueryableModel):
    path = 'workflows'
    data_key = 'Workflow'
    primary_key = ''
    fields = ()
    relationships = {
        'jobs': Job,
    }


class Service(base.QueryableModel):
    path = 'services'
    data_key = 'ServiceInfo'
    primary_key = 'service_name'
    fields = ('service_name', 'cluster_name', 'maintenance_state', 'state')
    relationships = {
        'components': Component,
    }

class Configuration(base.QueryableModel):
    path = 'configurations'
    data_key = 'Config'
    primary_key = 'type'
    fields = ('cluster_name', 'tag', 'type', 'version', 'properties')

    def load(self, response):
        # sigh, this API does not follow the pattern at all
        for field in self.fields:
            if field in response and field not in response['Config']:
                LOG.error("Config has %s in main response body", field)
                response['Config'][field] = response.pop(field)
        return super(Configuration, self).load(response)


class StackConfiguration(Configuration):
    data_key = 'StackConfigurations'
    fields = ('property_name', 'service_name', 'stack_name', 'stack_version',
              'final', 'property_description', 'property_type', 'property_value',
              'type')


class StackConfigurationList(Configuration):
    def __init__(self, *args, **kwargs):
        super(StackConfigurationList, self).__init__(*args, **kwargs)
        self.files = []
        self.iter_marker = 0

    def __iter__(self):
        self.inflate()
        return self

    def next(self):
        if self.iter_marker >= len(self.files):
            raise StopIteration
        model = self.files[self.iter_marker]
        self.iter_marker += 1
        return model

    def load(self, response):
        models = []
        # we either get a single item or a list of items.  WTF Ambari devs?
        if isinstance(response, list):
            for item in response:
                model = StackConfiguration(self.parent)
                model.load(item)
                models.append(model)
        else:
            model = StackConfiguration(self.parent)
            model.load(response)
            models.append(model)

        self.files = models


class Cluster(base.QueryableModel):
    path = 'clusters'
    data_key = 'Clusters'
    primary_key = 'cluster_name'
    fields = ('cluster_id', 'cluster_name', 'health_report', 'provisioning_state',
              'total_hosts', 'version', 'desired_configs',
              'desired_service_config_versions')
    relationships = {
        'hosts': ClusterHost,
        'requests': Request,
        'services': Service,
        'configurations': Configuration,
        'workflows': Workflow,
    }


class BlueprintHostGroup(base.DependentModel):
    fields = ('name', 'configurations', 'components')
    primary_key = 'name'


class Blueprint(base.QueryableModel):
    path = 'blueprints'
    data_key = 'Blueprints'
    primary_key = 'blueprint_name'
    fields = ('blueprint_name', 'stack_name', 'stack_version')
    relationships = {
        'host_groups': BlueprintHostGroup,
    }


class StackServiceComponent(Component):
    data_key = 'StackServiceComponents'
    primary_key = 'component_name'
    fields = ('component_name', 'service_name', 'stack_name', 'stack_version',
              'cardinality', 'component_category', 'custom_commands',
              'display_name', 'is_client', 'is_master')


class StackService(base.QueryableModel):
    path = 'services'
    data_key = 'StackServices'
    primary_key = 'service_name'
    fields = ('service_name', 'stack_name', 'stack_version', 'display_name',
              'comments', 'custom_commands', 'required_services',
              'service_check_supported', 'service_version', 'user_name',
              'config_types')
    relationships = {
        'components': StackServiceComponent,
        'configurations': StackConfigurationList,
    }


class Repository(base.QueryableModel):
    path = 'repositories'
    data_key = 'Repositories'
    primary_key = 'repo_id'
    fields = ('repo_id', 'repo_name', 'os_type', 'stack_name', 'stack_version',
              'base_url', 'default_base_url', 'latest_base_url', 'mirrors_list')


class OperatingSystem(base.QueryableModel):
    path = 'operatingSystems'
    data_key = 'OperatingSystems'
    primary_key = 'os_type'
    fields = ('os_type', 'stack_name', 'stack_version')
    relationships = {
        'repositories': Repository,
    }


class Version(base.QueryableModel):
    path = 'versions'
    data_key = 'Versions'
    primary_key = 'stack_version'
    fields = ('stack_name', 'stack_version', 'active', 'min_upgrade_version',
              'parent_stack_version', 'config_types')
    relationships = {
        'operating_systems': OperatingSystem,
        'services': StackService,
    }


class Stack(base.QueryableModel):
    path = 'stacks'
    data_key = 'Stacks'
    primary_key = 'stack_name'
    fields = ('stack_name')
    relationships = {
        'versions': Version,
    }


class UserPrivilege(base.QueryableModel):
    path = 'privileges'
    data_key = 'PrivilegeInfo'
    primary_key = 'privilege_id'
    fields = ('privilege_id', 'permission_name', 'principal_name',
              'principal_type', 'type', 'user_name', 'cluster_name')


class User(base.QueryableModel):
    path = 'users'
    data_key = 'Users'
    primary_key = 'user_name'
    fields = ('user_name', 'active', 'admin', 'groups', 'ldap_user', 'password')
    relationships = {
        'privileges': UserPrivilege,
    }


class GroupMember(base.QueryableModel):
    path = 'members'
    data_key = 'MemberInfo'
    primary_key = 'user_name'
    fields = ('user_name', 'group_name')


class Group(base.QueryableModel):
    path = 'groups'
    data_key = 'Groups'
    primary_key = 'group_name'
    fields = ('group_name', 'ldap_group')
    relationships = {
        'members': GroupMember,
    }


class ViewPermission(base.QueryableModel):
    path = 'permissions'
    data_key = 'PermissionInfo'
    primary_key = 'permission_id'
    fields = ('permission_id', 'version', 'view_name', 'permission_name',
              'resource_name')


class ViewInstance(base.QueryableModel):
    path = 'instances'
    data_key = 'ViewInstanceInfo'
    primary_key = 'instance_name'
    fields = ('instance_name', 'context_path', 'description', 'icon64_path',
              'icon_path', 'label', 'static', 'version', 'view_name', 'visible',
              'instance_data', 'properties')
    # privileges and resources relationships exist, but no idea how they're defined


class ViewVersion(base.QueryableModel):
    path = 'versions'
    data_key = 'ViewVersionInfo'
    primary_key = 'version'
    fields = ('version', 'view_name', 'archive', 'description', 'label',
              'masker_class', 'parameters', 'status', 'status_detail', 'system')
    relationships = {
        'permissions': ViewPermission,
        'instances': ViewInstance,
    }


class View(base.QueryableModel):
    path = 'views'
    data_key = 'ViewInfo'
    primary_key = 'view_name'
    fields = ('view_name')
    relationships = {
        'versions': ViewVersion,
    }


class RootServiceComponent(Component):
    data_key = 'RootServiceComponents'


class RootService(Service):
    data_key = 'RootService'
    fields = ('service_name')
    relationships = {
        'components': RootServiceComponent,
    }

