# Copyright 2015-2018 Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import mock
import pytest
from boto3.exceptions import Boto3Error
from ruamel.yaml import YAML

from paasta_tools.cli.cmds.spark_run import configure_and_run_docker_container
from paasta_tools.cli.cmds.spark_run import create_spark_config_str
from paasta_tools.cli.cmds.spark_run import DEFAULT_SERVICE
from paasta_tools.cli.cmds.spark_run import emit_resource_requirements
from paasta_tools.cli.cmds.spark_run import get_aws_credentials
from paasta_tools.cli.cmds.spark_run import get_docker_cmd
from paasta_tools.cli.cmds.spark_run import get_docker_run_cmd
from paasta_tools.cli.cmds.spark_run import get_spark_config
from paasta_tools.cli.cmds.spark_run import load_aws_credentials_from_yaml
from paasta_tools.utils import InstanceConfig
from paasta_tools.utils import SystemPaastaConfig


@mock.patch('paasta_tools.cli.cmds.spark_run.os.geteuid', autospec=True)
@mock.patch('paasta_tools.cli.cmds.spark_run.os.getegid', autospec=True)
def test_get_docker_run_cmd(
    mock_getegid,
    mock_geteuid,
):
    mock_geteuid.return_value = 1234
    mock_getegid.return_value = 100

    container_name = 'fake_name'
    volumes = ['v1:v1:rw', 'v2:v2:rw']
    env = {'k1': 'v1', 'k2': 'v2'}
    docker_img = 'fake-registry/fake-service'
    docker_cmd = 'pyspark'

    actual = get_docker_run_cmd(
        container_name,
        volumes,
        env,
        docker_img,
        docker_cmd,
    )

    assert actual[5:] == [
        '--user=1234:100',
        '--name=fake_name',
        '--env', 'k1=v1', '--env', 'k2=v2',
        '--volume=v1:v1:rw', '--volume=v2:v2:rw',
        'fake-registry/fake-service',
        'sh', '-c', 'pyspark', {},
    ]


@mock.patch('paasta_tools.cli.cmds.spark_run.find_mesos_leader', autospec=True)
@mock.patch('paasta_tools.cli.cmds.spark_run._load_mesos_secret', autospec=True)
def test_get_spark_config(
    mock_load_mesos_secret,
    mock_find_mesos_leader,
):
    mock_find_mesos_leader.return_value = 'fake_leader'
    args = mock.MagicMock()
    args.cluster = 'fake_cluster'

    spark_conf = get_spark_config(
        args=args,
        spark_app_name='fake_name',
        spark_ui_port=123,
        docker_img='fake-registry/fake-service',
        system_paasta_config=SystemPaastaConfig(
            {"cluster_fqdn_format": "paasta-{cluster:s}.something"},
            'fake_dir',
        ),
        volumes=['v1:v1:rw', 'v2:v2:rw'],
    )

    assert spark_conf['spark.master'] == 'mesos://fake_leader:5050'
    assert 'spark.master=mesos://fake_leader:5050' in create_spark_config_str(spark_conf)


@mock.patch('paasta_tools.cli.cmds.spark_run.get_aws_credentials', autospec=True)
@mock.patch('paasta_tools.cli.cmds.spark_run.os.path.exists', autospec=True)
@mock.patch('paasta_tools.cli.cmds.spark_run.pick_random_port', autospec=True)
@mock.patch('paasta_tools.cli.cmds.spark_run.get_username', autospec=True)
@mock.patch('paasta_tools.cli.cmds.spark_run.get_spark_config', autospec=True)
@mock.patch('paasta_tools.cli.cmds.spark_run.create_spark_config_str', autospec=True)
@mock.patch('paasta_tools.cli.cmds.spark_run.run_docker_container', autospec=True)
@mock.patch('time.time', autospec=True)
class TestConfigureAndRunDockerContainer:

    instance_config = InstanceConfig(
        cluster='fake_cluster',
        instance='fake_instance',
        service='fake_service',
        config_dict={
            'extra_volumes': [{
                "hostPath": "/h1",
                "containerPath": "/c1",
                "mode": "RO",
            }],
        },
        branch_dict={'docker_image': 'fake_service:fake_sha'},
    )

    system_paasta_config = SystemPaastaConfig(
        {
            'volumes': [{
                "hostPath": "/h2",
                "containerPath": "/c2",
                "mode": "RO",
            }],
        },
        'fake_dir',
    )

    def test_configure_and_run_docker_container(
        self,
        mock_time,
        mock_run_docker_container,
        mock_create_spark_config_str,
        mock_get_spark_config,
        mock_get_username,
        mock_pick_random_port,
        mock_os_path_exists,
        mock_get_aws_credentials,
    ):
        mock_pick_random_port.return_value = 123
        mock_get_username.return_value = 'fake_user'
        mock_create_spark_config_str.return_value = '--conf spark.app.name=fake_app'
        mock_run_docker_container.return_value = 0
        mock_get_aws_credentials.return_value = ('id', 'secret')

        args = mock.MagicMock()
        args.cluster = 'fake_cluster'
        args.cmd = 'pyspark'
        args.work_dir = '/fake_dir:/spark_driver'
        args.dry_run = True

        retcode = configure_and_run_docker_container(
            args=args,
            docker_img='fake-registry/fake-service',
            instance_config=self.instance_config,
            system_paasta_config=self.system_paasta_config,
        )

        assert retcode == 0
        mock_run_docker_container.assert_called_once_with(
            container_name='paasta_spark_run_fake_user_123',
            volumes=[
                '/h1:/c1:ro',
                '/h2:/c2:ro',
                '/fake_dir:/spark_driver:rw',
                '/etc/passwd:/etc/passwd:ro',
                '/etc/group:/etc/group:ro',
            ],
            environment={
                'PAASTA_SERVICE': 'fake_service',
                'PAASTA_INSTANCE': 'fake_instance',
                'PAASTA_CLUSTER': 'fake_cluster',
                'PAASTA_DEPLOY_GROUP': 'fake_cluster.fake_instance',
                'PAASTA_DOCKER_IMAGE': 'fake_service:fake_sha',
                'AWS_ACCESS_KEY_ID': 'id',
                'AWS_SECRET_ACCESS_KEY': 'secret',
                'SPARK_USER': 'root',
                'SPARK_OPTS': '--conf spark.app.name=fake_app',
            },
            docker_img='fake-registry/fake-service',
            docker_cmd='pyspark --conf spark.app.name=fake_app',
            dry_run=True,
        )

    def test_suppress_clusterman_metrics_errors(
        self,
        mock_time,
        mock_run_docker_container,
        mock_create_spark_config_str,
        mock_get_spark_config,
        mock_get_username,
        mock_pick_random_port,
        mock_os_path_exists,
        mock_get_aws_credentials,
    ):
        mock_get_aws_credentials.return_value = ('id', 'secret')

        with mock.patch(
            'paasta_tools.cli.cmds.spark_run.emit_resource_requirements', autospec=True,
        ) as mock_emit_resource_requirements, mock.patch(
            'paasta_tools.cli.cmds.spark_run.clusterman_metrics', autospec=True,
        ):
            mock_emit_resource_requirements.side_effect = Boto3Error
            mock_create_spark_config_str.return_value = '--conf spark.cores.max=5'

            args = mock.MagicMock(
                suppress_clusterman_metrics_errors=False,
                cmd='pyspark',
            )
            with pytest.raises(Boto3Error):
                configure_and_run_docker_container(
                    args=args,
                    docker_img='fake-registry/fake-service',
                    instance_config=self.instance_config,
                    system_paasta_config=self.system_paasta_config,
                )

            # make sure we don't blow up when this setting is True
            args.suppress_clusterman_metrics_errors = True
            configure_and_run_docker_container(
                args=args,
                docker_img='fake-registry/fake-service',
                instance_config=self.instance_config,
                system_paasta_config=self.system_paasta_config,
            )

    def test_dont_emit_metrics_for_inappropriate_commands(
        self,
        mock_time,
        mock_run_docker_container,
        mock_create_spark_config_str,
        mock_get_spark_config,
        mock_get_username,
        mock_pick_random_port,
        mock_os_path_exists,
        mock_get_aws_credentials,
    ):
        mock_get_aws_credentials.return_value = ('id', 'secret')
        with mock.patch(
            'paasta_tools.cli.cmds.spark_run.emit_resource_requirements', autospec=True,
        ) as mock_emit_resource_requirements, mock.patch(
            'paasta_tools.cli.cmds.spark_run.clusterman_metrics', autospec=True,
        ):
            mock_create_spark_config_str.return_value = '--conf spark.cores.max=5'
            args = mock.MagicMock(cmd='bash')

            configure_and_run_docker_container(
                args=args,
                docker_img='fake-registry/fake-service',
                instance_config=self.instance_config,
                system_paasta_config=self.system_paasta_config,
            )
            assert not mock_emit_resource_requirements.called


def test_emit_resource_requirements(tmpdir):
    spark_config_dict = {
        'spark.executor.cores': '2',
        'spark.cores.max': '4',
        'spark.executor.memory': '4g',
        'spark.app.name': 'paasta_spark_run_johndoe_2_3',
        'spark.mesos.constraints': 'pool:cool-pool\\;other:value',
    }

    clusterman_yaml_contents = {
        'mesos_clusters': {
            'anywhere-prod': {
                'aws_region': 'us-north-14',
            },
        },
    }
    clusterman_yaml_file_path = tmpdir.join('fake_clusterman.yaml')
    with open(clusterman_yaml_file_path, 'w') as f:
        YAML().dump(clusterman_yaml_contents, f)

    with mock.patch(
        'paasta_tools.cli.cmds.spark_run.get_clusterman_metrics', autospec=True,
    ), mock.patch(
        'paasta_tools.cli.cmds.spark_run.clusterman_metrics', autospec=True,
    ) as mock_clusterman_metrics, mock.patch(
        'paasta_tools.cli.cmds.spark_run.CLUSTERMAN_YAML_FILE_PATH',
        clusterman_yaml_file_path,
        autospec=None,  # we're replacing this name, so we can't autospec
    ), mock.patch(
        'time.time', return_value=1234, autospec=True,
    ):
        mock_clusterman_metrics.generate_key_with_dimensions.side_effect = lambda name, dims: (
            f'{name}|framework_name={dims["framework_name"]},webui_url={dims["webui_url"]}'
        )

        emit_resource_requirements(spark_config_dict, 'anywhere-prod', 'http://spark.yelp')

        mock_clusterman_metrics.ClustermanMetricsBotoClient.assert_called_once_with(
            region_name='us-north-14',
            app_identifier='cool-pool',
        )
        metrics_writer = mock_clusterman_metrics.ClustermanMetricsBotoClient.return_value.\
            get_writer.return_value.__enter__.return_value

        metric_key_template = (
            'requested_{resource}|framework_name=paasta_spark_run_johndoe_2_3,webui_url=http://spark.yelp'
        )
        metrics_writer.send.assert_has_calls(
            [
                mock.call((metric_key_template.format(resource='cpus'), 1234, 4)),
                mock.call((metric_key_template.format(resource='mem'), 1234, 8000)),
                mock.call((metric_key_template.format(resource='disk'), 1234, 8000)),
            ],
            any_order=True,
        )


def test_get_docker_cmd_add_spark_conf_str():
    args = mock.Mock(cmd='pyspark -v')
    instance_config = None
    spark_conf_str = '--conf spark.app.name=fake_app'

    docker_cmd = get_docker_cmd(args, instance_config, spark_conf_str)
    assert docker_cmd == 'pyspark --conf spark.app.name=fake_app -v'


def test_get_docker_cmd_other_cmd():
    args = mock.Mock(cmd='bash')
    instance_config = None
    spark_conf_str = '--conf spark.app.name=fake_app'

    assert get_docker_cmd(args, instance_config, spark_conf_str) == 'bash'


def test_load_aws_credentials_from_yaml(tmpdir):
    fake_access_key_id = 'fake_access_key_id'
    fake_secret_access_key = 'fake_secret_access_key'
    yaml_file = tmpdir.join('test.yaml')
    yaml_file.write(
        f'aws_access_key_id: "{fake_access_key_id}"\n'
        f'aws_secret_access_key: "{fake_secret_access_key}"',
    )

    aws_access_key_id, aws_secret_access_key = load_aws_credentials_from_yaml(yaml_file)
    assert aws_access_key_id == fake_access_key_id
    assert aws_secret_access_key == fake_secret_access_key


class TestGetAwsCredentials:

    @pytest.fixture(autouse=True)
    def mock_load_aws_credentials_from_yaml(self):
        with mock.patch(
            'paasta_tools.cli.cmds.spark_run.load_aws_credentials_from_yaml',
            autospec=True,
        ) as self.mock_load_aws_credentials_from_yaml:
            yield

    def test_yaml_provided(self):
        args = mock.Mock(aws_credentials_yaml='credentials.yaml')
        credentials = get_aws_credentials(args)

        self.mock_load_aws_credentials_from_yaml.assert_called_once_with('credentials.yaml')
        assert credentials == self.mock_load_aws_credentials_from_yaml.return_value

    @mock.patch('paasta_tools.cli.cmds.spark_run.os', autospec=True)
    @mock.patch('paasta_tools.cli.cmds.spark_run.get_service_aws_credentials_path', autospec=True)
    def test_service_provided_no_yaml(self, mock_get_credentials_path, mock_os):
        args = mock.Mock(aws_credentials_yaml=None, service='service_name')
        mock_os.path.exists.return_value = True
        credentials = get_aws_credentials(args)

        mock_get_credentials_path.assert_called_once_with(args.service)
        self.mock_load_aws_credentials_from_yaml.assert_called_once_with(
            mock_get_credentials_path.return_value,
        )
        assert credentials == self.mock_load_aws_credentials_from_yaml.return_value

    @mock.patch('paasta_tools.cli.cmds.spark_run.Session.get_credentials', autospec=True)
    def test_use_default_creds(self, mock_get_credentials):
        args = mock.Mock(aws_credentials_yaml=None, service=DEFAULT_SERVICE)
        mock_get_credentials.return_value = mock.MagicMock(access_key='id', secret_key='secret')
        credentials = get_aws_credentials(args)

        assert credentials == ('id', 'secret')

    @mock.patch('paasta_tools.cli.cmds.spark_run.os', autospec=True)
    @mock.patch('paasta_tools.cli.cmds.spark_run.Session.get_credentials', autospec=True)
    def test_service_provided_fallback_to_default(self, mock_get_credentials, mock_os):
        args = mock.Mock(aws_credentials_yaml=None, service='service_name')
        mock_os.path.exists.return_value = False
        mock_get_credentials.return_value = mock.MagicMock(access_key='id', secret_key='secret')
        credentials = get_aws_credentials(args)

        assert credentials == ('id', 'secret')
