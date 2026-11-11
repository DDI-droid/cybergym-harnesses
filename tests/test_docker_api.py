"""Real Docker acceptance using harmless agents: no model or grading requests."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'common'))
from docker_api import DockerAgent


class Echo:
    def __init__(self, text='hello', mode='ok'):
        self.text, self.mode = text, mode

    def solve(self, task, box, lm):
        import json
        import os
        import time
        assert not os.path.exists('/var/run/docker.sock')
        assert os.environ.get('ONI_TEST_HOST_SECRET') is None
        box.write('/tmp/note', self.text)
        assert box.read('/tmp/note').decode() == self.text
        result = box.run('printf from-container')
        assert result.output == 'from-container'
        box.note('local file and command checks passed')
        if self.mode == 'error':
            raise ValueError('deliberate fixture failure')
        if self.mode == 'sleep':
            time.sleep(30)
        if self.mode == 'none':
            return None
        if self.mode == 'direct':
            box.write('/output/poc', bytes([0, 255, 65]))
            return None
        if self.mode == 'command-timeout':
            result = box.run('sleep 30', timeout=0.1)
            assert result.timed_out and result.exit_code == 124
        return json.dumps({'text': self.text, 'hostname': os.uname().nodename,
                           'task': task.id, 'pid': os.getpid()}).encode()


class DockerAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import docker
        cls.client = docker.from_env()
        cls.client.ping()
        cls.temp = tempfile.TemporaryDirectory(prefix='cybergym-docker-api-')
        cls.root = Path(cls.temp.name)
        cls.images = []
        cls.harness = DockerAgent(Echo).build(cls.root / 'class-harness', name='api-test-class')
        cls.images.append(cls.harness.image)

    @classmethod
    def tearDownClass(cls):
        for image in cls.images:
            cls.client.images.remove(image, force=True)
        cls.client.close()
        cls.temp.cleanup()

    def run_agent(self, *, harness=None, mode='ok', timeout=10):
        harness = harness or self.harness
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            root = Path(tmp); workspace = root / 'workspace'; workspace.mkdir()
            output = root / 'output'
            with mock.patch.dict(os.environ, {'ONI_TEST_HOST_SECRET': 'not-forwarded'}):
                outcome = harness.run(workspace, output, settings={'text': 'fixture', 'mode': mode}, timeout=timeout)
            report = json.loads((output / 'agent_exit.json').read_text()) if (output / 'agent_exit.json').exists() else {}
            data = (output / 'poc').read_bytes() if (output / 'poc').exists() else None
            trajectory = (output / 'trajectory.jsonl').read_text()
            return outcome, report, data, trajectory

    def test_class_really_runs_in_container_with_settings_and_no_host_secrets(self):
        outcome, report, data, trace = self.run_agent()
        self.assertEqual(outcome['exit_code'], 0)
        self.assertEqual(report['exit_code'], 0)
        self.assertEqual(json.loads(data)['text'], 'fixture')
        self.assertNotEqual(json.loads(data)['hostname'], os.uname().nodename)
        self.assertEqual(json.loads(data)['task'], 'local')
        self.assertIn('local file and command checks passed', trace)
        self.assertEqual(outcome['image_id'], self.harness.image_id)
        self.assertEqual(self.client.containers.list(all=True, filters={'label': f"execution={outcome['execution_id']}"}), [])

    def test_file_class_is_imported_only_in_container_and_bundles_sibling_modules(self):
        source = self.root / 'user-source'; source.mkdir()
        marker = source / 'host-import-marker'
        (source / 'helpers.py').write_text('WORD = "from-file"\n')
        (source / 'agent.py').write_text(
            f'from pathlib import Path\nPath({str(marker)!r}).write_text("imported")\n'
            'from helpers import WORD\nclass FileAgent:\n'
            ' def __init__(self, **settings): pass\n'
            ' def solve(self, task, box, lm): return WORD.encode()\n')
        harness = DockerAgent.from_file(source / 'agent.py', 'FileAgent', source_root=source).build(self.root / 'file-harness', name='api-test-file')
        self.images.append(harness.image)
        self.assertFalse(marker.exists())
        # Use a safe import-time marker in a writable in-container location.
        # The host source absolute path does not exist in the container, so the
        # deliberate import attempt above must fail without touching the host.
        outcome, report, data, trace = self.run_agent(harness=harness)
        self.assertNotEqual(outcome['exit_code'], 0)
        self.assertIn('FileNotFoundError', report['error'])
        self.assertFalse(marker.exists())

    def test_successful_file_class(self):
        file = self.root / 'simple.py'
        file.write_text('class Agent:\n def __init__(self, **kwargs): pass\n def solve(self, task, box, lm): return b"file-ok"\n')
        harness = DockerAgent.from_file(file, 'Agent').build(self.root / 'file-ok', name='api-test-file-ok')
        self.images.append(harness.image)
        outcome, _, data, _ = self.run_agent(harness=harness)
        self.assertEqual(outcome['exit_code'], 0)
        self.assertEqual(data, b'file-ok')

    def test_agent_exception_is_visible(self):
        outcome, report, data, _ = self.run_agent(mode='error')
        self.assertEqual(outcome['exit_code'], 1)
        self.assertIn('deliberate fixture failure', report['error'])
        self.assertIsNone(data)

    def test_command_timeout_does_not_kill_the_agent(self):
        outcome, report, data, _ = self.run_agent(mode='command-timeout')
        self.assertEqual(outcome['exit_code'], 0)
        self.assertEqual(report['command_timeouts'], 1)

    def test_whole_agent_timeout_removes_container(self):
        outcome, _, _, _ = self.run_agent(mode='sleep', timeout=1)
        self.assertTrue(outcome['timed_out'])
        self.assertNotEqual(outcome['exit_code'], 0)
        self.assertEqual(self.client.containers.list(all=True, filters={'label': f"execution={outcome['execution_id']}"}), [])

    def test_direct_binary_output(self):
        outcome, report, data, _ = self.run_agent(mode='direct')
        self.assertEqual(outcome['exit_code'], 0)
        self.assertEqual(data, bytes([0, 255, 65]))

    def test_missing_output_is_an_explicit_failure(self):
        outcome, report, data, _ = self.run_agent(mode='none')
        self.assertEqual(outcome['exit_code'], 1)
        self.assertIn('without writing', report['error'])
        self.assertIsNone(data)

    def test_descriptor_survives_ray_serialization(self):
        from ray import cloudpickle
        from cybergym_eval import Gym
        Gym(env_file='').agent(self.harness)
        restored = cloudpickle.loads(cloudpickle.dumps(self.harness))
        self.assertEqual(restored.container_code({})['image_id'], self.harness.image_id)

    def test_changed_source_is_refused(self):
        file = Path(self.harness.directory) / 'agent.json'
        old = file.read_bytes()
        try:
            file.write_bytes(old + b' ')
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.harness.container_code({})
        finally:
            file.write_bytes(old)

    def test_build_refuses_overwrite(self):
        with self.assertRaises(FileExistsError):
            DockerAgent(Echo).build(self.harness.directory, name='again')

    def test_gym_accepts_container_descriptor_without_starting_services(self):
        from cybergym_eval import Gym
        agent = Gym(env_file='').agent(self.harness, text='one')
        self.assertIs(agent.container_harness, self.harness)
        self.assertNotEqual(agent.fingerprint, Gym(env_file='').agent(self.harness, text='two').fingerprint)

    def test_gym_rollout_routes_to_container_worker(self):
        from cybergym_eval import Gym
        from cybergym_eval.gym import TaskSet
        import cybergym_eval.gym as gm
        from cybergym_eval.worker import execute_wrapped
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            config = {'work_root': tmp, 'taskset': 'offline', 'difficulty': 'level2', 'split': 'all'}
            tasks = TaskSet('offline', ('arvo:1065',), config)
            agent = Gym(env_file='').agent(self.harness)
            def fake_run(config, specs, function, args):
                self.assertIs(function, execute_wrapped)
                self.assertEqual(args[0].image_id, self.harness.image_id)
                return {s.execution_id: {'status': 'benchmark_failed'} for s in specs}
            with mock.patch.object(gm, 'start_server'), mock.patch.object(gm, 'start_shim'), mock.patch('cybergym_eval.cli.preflight', return_value=0), mock.patch.object(gm, 'clean_leftovers'), mock.patch.object(gm, 'run_specs', side_effect=fake_run):
                result = agent.rollout(tasks, 'arvo:1065')
                self.assertEqual(result.status, 'benchmark_failed')

    def test_evaluator_routes_descriptor_through_shared_execution_core(self):
        from cybergym_eval import worker
        from cybergym_eval.config import RunSpec
        spec = RunSpec('arvo:1065', 'level2', 'test', 'offline', '/unused')
        with mock.patch.object(worker, '_execute', return_value={'status': 'offline'}) as run:
            worker.execute_wrapped({}, vars(spec), self.harness, {}, 'fingerprint')
            self.assertIsInstance(run.call_args.args[2], worker._WrappedContainerAgent)


if __name__ == '__main__':
    unittest.main(verbosity=2)
