"""Offline checks: no Kubernetes or Datadog connections are made."""
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import playground as demo


class ScopeChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out=Path(self.temp.name)
        self.scope=patch.object(demo,'OUT',self.out);self.scope.start();self.addCleanup(self.scope.stop)
        self.state={'context':'customer-context','node':'demo-node','hostname_label':'demo-node','cluster_name':'customer-security-demo','site':'datadoghq.eu'}

    def test_eu1_and_single_node_and_no_remote_rules(self):
        demo.render(self.state)
        values=json.loads((self.out/'agent-values.json').read_text())
        self.assertEqual(values['datadog']['site'],'datadoghq.eu')
        self.assertEqual(values['agents']['nodeSelector'],{'kubernetes.io/hostname':'demo-node'})
        self.assertFalse(values['datadog']['remoteConfiguration']['enabled'])
        self.assertFalse(values['datadog']['operator']['enabled'])
        self.assertFalse(values['datadog']['apm']['portEnabled'])

    def test_every_kill_requires_exact_current_container(self):
        pod={'metadata':{'name':'playground-app-test'},'spec':{'nodeName':'demo-node'},'status':{'containerStatuses':[{'containerID':'containerd://'+'a'*64,'ready':True}]}}
        with patch.object(demo,'ready_pod',return_value=pod),patch.object(demo,'kube') as kube:
            demo.policy(self.state)
        rules=json.loads((self.out/'playground.policy').read_text())['rules']
        kills=[(r,a) for r in rules for a in r.get('actions',[]) if 'kill' in a]
        self.assertEqual(len(kills),3)
        guard='container.id == "'+'a'*64+'"'
        for r,a in kills:
            self.assertIn(guard,r['expression']);self.assertEqual(a['filter'],guard)
            self.assertEqual(a['kill'],{'scope':'process','signal':'SIGKILL'})
        self.assertEqual(kube.call_args.args[1:3],('apply','-f'))

    def test_cleanup_checks_ownership_before_any_uninstall(self):
        foreign=type('R',(),{'stdout':json.dumps({'metadata':{'labels':{}}})})()
        with patch.object(demo,'kube',return_value=foreign),patch.object(demo,'run') as run:
            with self.assertRaises(SystemExit):demo.cleanup(self.state)
            run.assert_not_called()

    def test_existing_probe_stops_install_before_mutations(self):
        args=argparse.Namespace(context='customer-context',node='demo-node',cluster_name='customer-security-demo',insecure_kubelet_tls=False)
        node={'metadata':{'labels':{'kubernetes.io/os':'linux','kubernetes.io/arch':'amd64'}},'status':{'nodeInfo':{'osImage':'Ubuntu'}}}
        pods={'items':[{'metadata':{'namespace':'datadog','name':'existing-agent'},'spec':{'containers':[{'name':'system-probe','image':'registry.datadoghq.eu/agent:7.81.1'}]}}]}
        with patch.object(demo,'STATE',self.out/'state.json'),patch.object(demo.shutil,'which',return_value='/tool'),patch.object(demo,'get',side_effect=[node,pods]),patch.object(demo,'kube') as kube:
            with self.assertRaises(SystemExit):demo.preflight(args)
            kube.assert_not_called()


if __name__=='__main__':unittest.main()
