# This file is part of the sos project: https://github.com/sosreport/sos
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# version 2 of the GNU General Public License.
#
# See the LICENSE file in the source distribution for further information.

from sos.report.plugins import Plugin, RedHatPlugin
from fnmatch import translate
import os
import re


class Openshift(Plugin, RedHatPlugin):
    """This is the plugin for OCP 4.x collections. While this product is still
    built ontop of kubernetes, there is enough difference in the collection
    requirements and approach to warrant a separate plugin as opposed to
    further extending the kubernetes plugin (or the OCP 3.x extensions included
    in the Red Hat version of the kube plugin).

    By default, this plugin will collect cluster information and inspect the
    default namespaces/projects that are created during deployment - i.e. the
    namespaces of the cluster projects matching openshift.* and kube.*. At the
    time of this plugin's creation that number of default projects is already
    north of 50; hence this plugin is expected to take a long time in both the
    setup() and collect() phases. End-user projects may also be collected from
    when those projects are included in the `add-namespaces` or
    `only-namespaces` options.

    It is expected to need to perform an `oc login` command in order for this
    plugin to be able to correctly capture information, as system root is not
    considered cluster root on the cluster nodes in order to access the API.

    Users will need to either:

        1) Provide the bearer token via the `-k openshift.token` option
        2) Provide the bearer token via the `SOSOCPTOKEN` environment variable
        3) Otherwise ensure that the root user can successfully run `oc` and
           get proper output prior to running this plugin


    It is highly suggested that option #2 be used first, as this will prevent
    the token from being recorded in output saved to the archive. Option #1 may
    be used if this is considered an acceptable risk. It is not recommended to
    rely on option #3, though it will provide the functionality needed.
    """

    short_desc = 'Openshift Container Platform 4.x'

    plugin_name = "openshift"
    plugin_timeout = 900
    profiles = ('openshift',)
    packages = ('openshift-hyperkube',)

    option_list = [
        ('token', 'admin token to allow API queries', 'fast', None),
        ('host', 'host address to use for oc login, including port', 'fast',
         'https://localhost:6443'),
        ('no-oc', 'do not collect `oc` command output', 'fast', False),
        ('podlogs', 'collect logs from each pod', 'fast', True),
        ('podlogs-filter', ('limit podlogs collection to pods matching this '
         'regex'), 'fast', ''),
        ('only-namespaces', 'colon-delimited list of namespaces to collect',
         'fast', ''),
        ('add-namespaces', ('colon-delimited list of namespaces to add to the '
         'default collections'), 'fast', '')
    ]

    def _check_oc_function(self):
        """Check to see if we can run `oc` commands"""
        return self.exec_cmd('oc whoami')['status'] == 0

    def _check_oc_logged_in(self):
        """See if we're logged in to the API service, and if not attempt to do
        so using provided plugin options
        """
        if self._check_oc_function():
            return True

        # Not logged in currently, attempt to do so
        token = self.get_option('token') or os.getenv('SOSOCPTOKEN', None)
        if token:
            oc_res = self.exec_cmd("oc login %s --token=%s "
                                   "--insecure-skip-tls-verify=True"
                                   % (self.get_option('host'), token))
            if oc_res['status'] == 0:
                if self._check_oc_function():
                    return True

            self._log_warn("Attempt to login to OCP API failed, will not run "
                           "or collect `oc` commands")
            return False

        self._log_warn("Not logged in to OCP API, and no login token provided."
                       " Will not collect `oc` commands")
        return False

    def _setup_namespace_regexes(self):
        """Combine a set of regexes for collection with any namespaces passed
        to sos via the -k openshift.add-namespaces option. Note that this does
        allow for end users to specify namespace regexes of their own.
        """

        if self.get_option('only-namespaces'):
            return [n for n in self.get_option('only-namespaces').split(':')]

        collect_regexes = [
            'openshift.*',
            'kube.*'
        ]

        if self.get_option('add-namespaces'):
            for nsp in self.get_option('add-namespaces').split(':'):
                collect_regexes.append(nsp)

        return collect_regexes

    def _reduce_namespace_list(self, nsps):
        """Reduce the namespace listing returned to just the ones we want to
        collect from. By default, as requested by OCP support personnel, this
        must include all 'openshift' prefixed namespaces

            :param nsps list:            Namespace names from oc output
        """

        def _match_namespace(namespace):
            """Match a particular namespace for inclusion (or not) in the
            collection phases

                :param namespace str:   The name of a namespace
            """

            for regex in self.collect_regexes:
                if re.match(regex, namespace):
                    return True
            return False

        self.collect_regexes = self._setup_namespace_regexes()

        return list(set([n for n in nsps if _match_namespace(n)]))

    def setup(self):
        """The setup() phase of this plugin will iterate through all default
        projects (namespaces), and/or those specified via the `add-namespaces`
        and `only-namespaces` plugin options. Both of these options accept
        shell-style regexes.

        Cluster-wide information, that is information that is not tied to a
        specific namespace, will be saved in the top-level plugin directory.
        Each namespace will have it's own subdir within the `namespaces` subdir
        to aide in organization. From there, each namespace subdir will have a
        subsequent subdir for each type of API resource the plugin collects.

        In contrast with the `kubernetes` plugin, this plugin will collect
        logs from all pods within each namespace, as well as the previous pod's
        logs, by default. The `-k openshift.podlogs-filter` option can be used
        to greatly reduce the amount of collected information.
        """

        # Capture the kubelet journal, but don't use it as a service which
        # would simultaneously enable this and the kubernetes plugin
        self.add_journal('kubelet')
        self.add_service_status('kubelet')
        self.add_forbidden_path([
            '/etc/kubernetes/*.crt',
            '/etc/kubernetes/*.key',
        ])
        self.add_copy_spec('/etc/kubernetes/*')

        # see if we run `oc` commands
        if not self.get_option('no-oc'):
            can_run_oc = self._check_oc_logged_in()
        else:
            can_run_oc = False

        if can_run_oc:
            # with an out-of-the-box install, setup time alone has been known
            # to take over 5 minutes. Print a notification message so that
            # users don't prematurely think sos has hung during setup
            self._log_warn(
                'Note that the Openshift Container Platform plugin can be '
                'expected in most configurations to take 5+ minutes in both '
                'the setup and collection phases'
            )

            self.oc_cmd = "oc get "
            oc_nsps = []

            # get 'global' or cluster-level information
            self.add_cmd_output([
                'oc cluster-info',
                'oc get -A pv',
                'oc get -A csr',
                'oc status',
                'oc version'
            ])

            # get non-namespaces api resources
            self.collect_cluster_resources()

            # get all namespaces, as data collection will be organized by that
            _nm_res = self.collect_cmd_output("%s namespaces" % self.oc_cmd)
            if _nm_res['status'] == 0:
                nsps = [
                    n.split()[0] for n in _nm_res['output'].splitlines()[1:]
                ]
                oc_nsps = self._reduce_namespace_list(nsps)

            # collect each namespace individually
            for namespace in oc_nsps:
                self.collect_from_namespace(namespace)

    def collect_cluster_resources(self):
        """Collect cluster-level (non-namespaced) resources from the API
        """
        global_resources = [
            'clusternetworks',
            'clusteroperators',
            'clusterversions',
            'componentstatuses',
            'configs',
            'containerruntimeconfigs',
            'controllerconfigs',
            'dnses',
            'hostsubnets',
            'infrastructures',
            'machineconfigpools',
            'machineconfigs',
            'netnamespaces',
            'networks',
            'nodes',
            'proxies',
            'storageclasses'
        ]

        for resource in global_resources:
            _subdir = "cluster_resources/%s" % resource
            _res = self.collect_cmd_output("%s %s" % (self.oc_cmd, resource),
                                           subdir=_subdir)
            if _res['status'] == 0:
                for _res_name in _res['output'].splitlines()[1:]:
                    self.add_cmd_output(
                        "oc describe %s %s" % (resource, _res_name.split()[0]),
                        subdir=_subdir
                    )

    def collect_from_namespace(self, namespace):
        """Run through the collection routines for an individual namespace.
        This collection should include all requested resources that exist
        within that namesapce

            :param namespace str:           The name of the namespace
        """

        # define the list of resources to collect
        resources = [
            'buildconfigs',
            'builds',
            'catalogsourceconfigs',
            'catalogsources',
            'clusterserviceversions',
            'configmaps',
            'daemonsets',
            'deploymentconfigs',
            'deployments',
            'events',
            'horizontalpodautoscalers',
            'imagestreams',
            'ingresscontrollers',
            'ingresses',
            'installplans',
            'limitranges',
            'machines',
            'machinesets',
            'mcoconfigs',
            'net-attach-def',
            'operatorgroups',
            'operatorsources',
            'pods',
            'pvc',
            'resourcequotas',
            'routes',
            'secrets',
            'services',
            'statefulsets',
            'subscriptions'

        ]

        # save to namespace-specific subdirs to keep the plugin dir organized
        subdir = "namespaces/%s" % namespace

        # namespace-specific non-resource collections
        self.add_cmd_output("oc describe namespace %s" % namespace,
                            subdir=subdir)

        for res in resources:
            _subdir = "%s/%s" % (subdir, res)
            _get_cmd = "%s --namespace=%s %s" % (self.oc_cmd, namespace, res)
            # get the 'normal' output first
            _res_out = self.collect_cmd_output(
                _get_cmd,
                subdir=_subdir
            )

            # then get specific detail on each instance of the resource
            if _res_out['status'] == 0:
                _instances = _res_out['output'].splitlines()[1:]
                for _instance in _instances:
                    _instance_name = _instance.split()[0]
                    self.add_cmd_output(
                        "%s %s -o yaml" % (_get_cmd, _instance_name),
                        subdir=_subdir,
                        suggest_filename="%s.yaml" % _instance_name
                    )
                # check for podlogs here as a slight optimization to re-running
                # 'oc get pods' on all namespaces
                if res == 'pods' and _instances and self.get_option('podlogs'):
                    pod_list = [p.split()[0] for p in _instances]
                    self.collect_podlogs(namespace, pod_list)

    def collect_podlogs(self, namespace, pod_list):
        """For any namespace that has active pods in it, collect the current
        and previous pod's logs

            :param pod_list list:       A list of pod names
        """
        _log_dir = "namespaces/%s/pods/podlogs" % namespace

        if self.get_option('podlogs-filter'):
            # this allows shell-style regex which is more commonly known by
            # sysadmins than python-style regex
            regex = translate(self.get_option('podlogs-filter'))
        else:
            regex = None

        for pod in pod_list:
            if regex and not re.match(regex, pod):
                continue
            _log_cmd = "oc logs --namespace=%s %s" % (namespace, pod)
            self.add_cmd_output([
                _log_cmd,
                _log_cmd + " -p"
            ], subdir=_log_dir)

    def postproc(self):

        # clear any certificate output
        self.do_cmd_private_sub('oc ')
        self.do_file_private_sub('/etc/kubernetes/*')

        # clear the certificate data from /etc/kubernetes that does not have
        # the certificate banners that the _private_sub() methods look for
        _fields = [
            '.*.crt',
            'client-certificate-data',
            'client-key-data',
            'certificate-authority-data',
            '.*.key',
            'token',
            '.*token.*.value'  # don't blind match `.*token.*` and lose names
        ]

        regex = r'(\s*(%s):)(.*)' % '|'.join(_fields)

        self.do_path_regex_sub('/etc/kubernetes/*', regex, r'\1 *******')
        # scrub secret content
        self.do_cmd_output_sub('secrets', regex, r'\1 *******')

        # `oc describe` output can include url-encoded file content. For the
        # most part this is not important as the majority of these instances
        # are the contents of bash scripts. However, a select few can contain
        # actual data, so just scrub everything that matches the describe
        # format for this content
        regex = r'(?P<var>(.*\\n)?Source:\s(.*),)((.*?))\n'
        self.do_cmd_output_sub('oc describe', regex, r'\g<var> *******\n')

# vim: set et ts=4 sw=4 :
