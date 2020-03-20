# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import logging
import os
import re
import yaml

from tripleo_common.actions import parameters

from tripleoclient.constants import ANSIBLE_TRIPLEO_PLAYBOOKS
from tripleoclient.constants import UNUSED_PARAMETER_EXCLUDES_RE
from tripleoclient import exceptions
from tripleoclient import utils
from tripleoclient.workflows import base
from tripleoclient.workflows import roles


LOG = logging.getLogger(__name__)


def update_parameters(workflow_client, **input_):
    return base.call_action(workflow_client, 'tripleo.parameters.update',
                            **input_)


def invoke_plan_env_workflows(clients, stack_name, plan_env_file):
    """Invokes the workflows in plan environment file"""

    try:
        with open(plan_env_file) as pf:
            plan_env_data = yaml.safe_load(pf.read())
    except IOError as exc:
        raise exceptions.PlanEnvWorkflowError('File (%s) is not found: '
                                              '%s' % (plan_env_file, exc))

    if plan_env_data and "playbook_parameters" in plan_env_data:
        static_inventory = utils.get_tripleo_ansible_inventory(
            ssh_user='heat-admin',
            stack=stack_name,
            undercloud_connection='local',
            return_inventory_file_path=True
        )
        with utils.TempDirs() as tmp:
            for pb, pb_vars in plan_env_data["playbook_parameters"].items():
                print('Invoking playbook ({}) specified in plan-environment'
                      ' file'.format(pb))
                LOG.debug(
                    'Running playbook "{}" with the'
                    ' following options {}.'.format(
                        pb,
                        pb_vars
                    )
                )
                playbook_dir = os.path.dirname(pb)
                if not playbook_dir:
                    playbook_dir = ANSIBLE_TRIPLEO_PLAYBOOKS

                utils.run_ansible_playbook(
                    playbook=os.path.basename(pb),
                    inventory=static_inventory,
                    workdir=tmp,
                    playbook_dir=playbook_dir,
                    extra_vars=pb_vars
                )

    # NOTE(cloudnull): Remove this when mistral is gone.
    elif plan_env_data and "workflow_parameters" in plan_env_data:
        LOG.warning(
            'The workflow_parameters interface is deprecated, begin using'
            ' playbook_parameters instead.'
        )
        for wf_name, wf_inputs in plan_env_data["workflow_parameters"].items():
            print('Invoking workflow (%s) specified in plan-environment '
                  'file' % wf_name)
            inputs = {'plan': stack_name, 'user_inputs': wf_inputs}
            workflow_client = clients.workflow_engine
            tripleoclients = clients.tripleoclient
            with tripleoclients.messaging_websocket() as ws:
                execution = base.start_workflow(
                    workflow_client,
                    wf_name,
                    workflow_input=inputs
                )

                # Getting the derive parameters timeout after 600 seconds.
                for payload in base.wait_for_messages(workflow_client,
                                                      ws, execution, 600):
                    if ('message' in payload and
                            (payload.get('status', 'RUNNING') == "RUNNING")):
                        print(payload['message'])

            if payload.get('status', 'FAILED') == 'SUCCESS':
                result = payload.get('result', '')
                # Prints the workflow result
                if result:
                    print('Workflow execution is completed. result:')
                    print(yaml.safe_dump(result, default_flow_style=False))
            else:
                message = payload.get('message', '')
                msg = ('Workflow execution is failed: %s' % (message))
                raise exceptions.PlanEnvWorkflowError(msg)


def check_deprecated_parameters(clients, container):
    """Checks for deprecated parameters in plan and adds warning if present.

    :param clients: application client object.
    :type clients: Object

    :param container: Name of the stack container.
    :type container: String
    """

    context = clients.tripleoclient.create_mistral_context()
    role_name_list = roles.list_available_roles(
        clients=clients,
        container=container
    )
    flattened_parms = parameters.GetFlattenedParametersAction(
        container=container
    ).run(context=context)
    user_params = flattened_parms.get('environment_parameters', {})
    heat_resource_tree = flattened_parms.get('heat_resource_tree', {})
    heat_resource_tree_params = heat_resource_tree.get('parameters', {})
    heat_resource_tree_resources = heat_resource_tree.get('resources', {})
    plan_params = heat_resource_tree_params.keys()
    parameter_groups = [
        i.get('parameter_groups')
        for i in heat_resource_tree_resources.values()
        if i.get('parameter_groups')
    ]
    params_role_specific_tag = [
        i.get('name')
        for i in heat_resource_tree_params.values()
        if 'tags' in i and 'role_specific' in i['tags']
    ]

    r = re.compile(".*Count")
    filtered_names = list(filter(r.match, plan_params))
    valid_role_name_list = list()
    for name in filtered_names:
        default = heat_resource_tree_params[name].get('default', 0)
        if default and int(default) > 0:
            role_name = name.rstrip('Count')
            if [i for i in role_name_list if i.get('name') == role_name]:
                valid_role_name_list.append(role_name)

    deprecated_params = [
        i[0] for i in parameter_groups
        if i[0].get('label') == 'deprecated'
    ]
    # We are setting a frozenset here because python 3 complains that dict is
    # a unhashable type.
    # On user_defined, we check if the size is higher than 0 because an empty
    # frozenset still is a subset of a frozenset, so we can't use issubset
    # here.
    user_params_keys = frozenset(user_params.keys())
    deprecated_result = [
        {
            'parameter': i,
            'deprecated': True,
            'user_defined': len(
                [x for x in frozenset(i) if x in user_params_keys]) > 0
        }
        for i in deprecated_params
    ]
    unused_params = [i for i in user_params.keys() if i not in plan_params]
    user_provided_role_specific = [
        v for i in role_name_list
        for k, v in user_params.items()
        if k in i
    ]
    invalid_role_specific_params = [
        i for i in user_provided_role_specific
        if i in params_role_specific_tag
    ]
    deprecated_parameters = [
        param['parameter'] for param in deprecated_result
        if param.get('user_defined')
    ]

    if deprecated_parameters:
        deprecated_join = ', '.join(deprecated_parameters)
        LOG.warning(
            'WARNING: Following parameter(s) are deprecated and still '
            'defined. Deprecated parameters will be removed soon!'
            ' {deprecated_join}'.format(
                deprecated_join=deprecated_join
            )
        )

    # exclude our known params that may not be used
    ignore_re = re.compile('|'.join(UNUSED_PARAMETER_EXCLUDES_RE))
    unused_params = [p for p in unused_params if not ignore_re.search(p)]

    if unused_params:
        unused_join = ', '.join(unused_params)
        LOG.warning(
            'WARNING: Following parameter(s) are defined but not '
            'currently used in the deployment plan. These parameters '
            'may be valid but not in use due to the service or '
            'deployment configuration.'
            ' {unused_join}'.format(
                unused_join=unused_join
            )
        )

    if invalid_role_specific_params:
        invalid_join = ', '.join(invalid_role_specific_params)
        LOG.warning(
            'WARNING: Following parameter(s) are not supported as '
            'role-specific inputs. {invalid_join}'.format(
                invalid_join=invalid_join
            )
        )


def generate_fencing_parameters(clients, nodes_json, delay, ipmi_level,
                                ipmi_cipher, ipmi_lanplus):
    """Generate and return fencing parameters.

    :param clients: application client object.
    :type clients: Object

    :param nodes_json: list of nodes & attributes in json format
    :type nodes_json: List

    :param delay: time to wait before taking fencing action
    :type delay: Integer

    :param ipmi_level: IPMI user level to use
    :type ipmi_level: String

    :param ipmi_cipher: IPMI cipher suite to use
    :type ipmi_cipher: String

    :param ipmi_lanplus: whether to use IPMIv2.0
    :type ipmi_lanplus: Boolean

    :returns: Dictionary
    """
    context = clients.tripleoclient.create_mistral_context()
    fencing_params = parameters.GenerateFencingParametersAction(
        nodes_json=nodes_json,
        delay=delay,
        ipmi_level=ipmi_level,
        ipmi_cipher=ipmi_cipher,
        ipmi_lanplus=ipmi_lanplus
    )
    return fencing_params.run(context=context)
