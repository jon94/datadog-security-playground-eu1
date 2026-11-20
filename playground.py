#!/usr/bin/env python3
"""Portable EU1 Workload Protection demo. No third-party Python dependencies."""
import argparse
import getpass
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'generated'
STATE = OUT / 'state.json'
CHART_VERSION = '3.231.4'
RELEASE = 'security-playground'
AGENT_NS = 'playground-security'
APP_NS = 'playground'
OWNER = 'workload-protection-playground'


def fail(message):
    raise SystemExit(message)


def run(args, data=None, capture=False, env=None):
    return subprocess.run(args, input=data, capture_output=capture,
                          text=not isinstance(data, bytes), check=True, env=env)


def kube(state, *args, data=None, capture=False):
    return run(['kubectl', '--context', state['context'], *args], data, capture)


def get(state, *args):
    return json.loads(kube(state, *args, '-o', 'json', capture=True).stdout)


def save(name, obj):
    OUT.mkdir(exist_ok=True)
    p = OUT / name
    p.write_text(json.dumps(obj, indent=2) + '\n')
    return p


def load_state():
    if not STATE.exists():
        fail('Run install first. This directory has no generated/state.json.')
    return json.loads(STATE.read_text())


def api(state, method, path, body=None, app_key=False):
    key = os.getenv('DD_API_KEY') or getpass.getpass('EU1 Datadog API key: ')
    headers = {'DD-API-KEY': key, 'Content-Type': 'application/json'}
    if app_key:
        headers['DD-APPLICATION-KEY'] = os.getenv('DD_APP_KEY') or getpass.getpass('EU1 Datadog application key: ')
    request = urllib.request.Request('https://api.' + state['site'] + path,
        headers=headers, method=method,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response), key
    except urllib.error.HTTPError as e:
        fail('Datadog API HTTP %s: %s' % (e.code, e.read().decode()[:1000]))


def render(state):
    values = json.loads((ROOT / 'templates/agent-values.json').read_text())
    values['datadog']['site'] = state['site']
    values['datadog']['clusterName'] = state['cluster_name']
    values['datadog']['kubelet']['tlsVerify'] = not state.get('insecure_kubelet_tls', False)
    values['agents']['nodeSelector'] = {'kubernetes.io/hostname': state['hostname_label']}
    if state.get('gke_cos'):
        values['providers'] = {'gke': {'cos': True}}
    workloads = json.loads((ROOT / 'templates/workloads.json').read_text().replace('__DEMO_NODE__', state['hostname_label']))
    save('agent-values.json', values)
    save('workloads.json', workloads)
    return workloads


def preflight(args):
    for tool in ['kubectl', 'helm']:
        if not shutil.which(tool):
            fail('Required command not found: ' + tool)
    state = {'context': args.context, 'node': args.node, 'cluster_name': args.cluster_name,
             'site': 'datadoghq.eu', 'insecure_kubelet_tls': args.insecure_kubelet_tls}
    if STATE.exists():
        old = load_state()
        if any(old[k] != state[k] for k in ['context', 'node', 'cluster_name', 'site']):
            fail('Existing state points to another deployment. Use a separate copy of the kit.')
        state.update(old)
        state['insecure_kubelet_tls'] = args.insecure_kubelet_tls
    node = get(state, 'get', 'node', args.node)
    labels = node['metadata'].get('labels', {})
    if labels.get('kubernetes.io/os') != 'linux' or labels.get('kubernetes.io/arch') != 'amd64':
        fail('This tested recipe requires a standard Linux amd64 node. ARM64 needs a separately verified image/binary path.')
    if node.get('spec', {}).get('unschedulable'):
        fail('Selected node is unschedulable.')
    if any(t.get('effect') in ['NoSchedule', 'NoExecute'] for t in node.get('spec', {}).get('taints', [])):
        fail('Selected node has scheduling taints. Choose an untainted demo node for this recipe.')
    state['hostname_label'] = labels.get('kubernetes.io/hostname', args.node)
    state['gke_cos'] = 'Container-Optimized OS' in node['status']['nodeInfo'].get('osImage', '')
    pods = get(state, 'get', 'pods', '-A', '--field-selector', 'spec.nodeName=' + args.node)['items']
    for pod in pods:
        ns = pod['metadata']['namespace']
        if ns == AGENT_NS and pod['metadata'].get('labels', {}).get('app') == RELEASE+'-datadog':
            continue
        for c in pod['spec'].get('containers', []):
            if c['name'] in ['system-probe', 'security-agent'] and 'datadog' in c.get('image', ''):
                fail('An existing Datadog probe/security Agent is on this node. Choose another demo node or have the platform owner integrate the policy into that installation; do not deploy overlapping probes.')
    for ns in [APP_NS, AGENT_NS]:
        result = get(state, 'get', 'namespace', ns, '--ignore-not-found') if kube(state, 'get', 'namespace', ns, '--ignore-not-found', capture=True).stdout.strip() else None
        if result and result['metadata'].get('labels', {}).get('security-demo') != OWNER:
            fail('Namespace already exists and is not owned by this kit: ' + ns)
    print('Target context:', state['context'])
    print('Target node:', state['node'])
    print('Datadog site:', state['site'])
    print('Only the kit namespaces and a separate node-selected Agent release will be changed.')
    return state


def ready_pod(state):
    pods = get(state, '-n', APP_NS, 'get', 'pods', '-l', 'app=playground-app')['items']
    for pod in pods:
        statuses = pod.get('status', {}).get('containerStatuses', [])
        if not pod['metadata'].get('deletionTimestamp') and statuses and all(c.get('ready') for c in statuses):
            if pod['spec']['nodeName'] != state['node']:
                fail('The playground pod is not on the selected demo node.')
            return pod
    fail('No ready playground pod. Inspect kubectl get pods -n playground.')


def policy(state):
    pod = ready_pod(state)
    state['container_id'] = pod['status']['containerStatuses'][0]['containerID'].split('://', 1)[1]
    state['pod'] = pod['metadata']['name']
    template = (ROOT / 'templates/playground.policy.json').read_text()
    text = template.replace('__PLAYGROUND_CONTAINER_ID__', state['container_id'])
    data = json.loads(text)
    guard = 'container.id == ' + json.dumps(state['container_id'])
    for rule in data['rules']:
        if guard not in rule['expression']:
            fail('Rule lacks exact playground container guard: ' + rule['id'])
        for action in rule.get('actions', []):
            if 'kill' in action and (action.get('filter') != guard or action['kill'] != {'signal': 'SIGKILL', 'scope': 'process'}):
                fail('Unexpected kill action scope.')
    save('playground.policy', data)
    cm = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'playground-blocking-policy', 'namespace': AGENT_NS}, 'data': {'playground.policy': json.dumps(data, indent=2)}}
    save('policy-configmap.json', cm)
    kube(state, 'apply', '-f', '-', data=json.dumps(cm))
    save('state.json', state)


def install(args):
    state = preflight(args)
    valid, key = api(state, 'GET', '/api/v1/validate')
    if not valid.get('valid'):
        fail('The API key is not valid in EU1.')
    workloads = render(state)
    save('state.json', state)
    namespaces = workloads['items'][:2]
    kube(state, 'apply', '-f', '-', data=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': namespaces}))
    secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'playground-datadog-api', 'namespace': AGENT_NS}, 'type': 'Opaque', 'stringData': {'api-key': key}}
    kube(state, 'apply', '--server-side', '--field-manager=security-playground-kit', '-f', '-', data=json.dumps(secret))
    del key
    kube(state, 'apply', '-f', str(OUT/'workloads.json'))
    kube(state, '-n', APP_NS, 'rollout', 'status', 'deployment/playground-app', '--timeout=300s')
    policy(state)
    chart = OUT / ('datadog-' + CHART_VERSION + '.tgz')
    if not chart.exists():
        run(['helm', 'pull', 'datadog', '--repo', 'https://helm.datadoghq.com', '--version', CHART_VERSION, '--destination', str(OUT)])
    env = dict(os.environ, HELM_DRIVER='secret')
    run(['helm', 'upgrade', '--install', RELEASE, str(chart), '--namespace', AGENT_NS,
         '--kube-context', state['context'], '-f', str(OUT/'agent-values.json'), '--wait', '--timeout', '8m'], env=env)
    print('Installed. Next: python3 playground.py status, then create-signal-rule, then test.')


def status(state):
    kube(state, '-n', APP_NS, 'get', 'pods', '-o', 'wide')
    kube(state, '-n', AGENT_NS, 'get', 'pods', '-o', 'wide')
    kube(state, '-n', AGENT_NS, 'exec', 'daemonset/'+RELEASE+'-datadog', '-c', 'security-agent', '--', 'security-agent', 'status')


def create_signal(state, state_filename='state.json'):
    if state.get('signal_rule_id'):
        print('Detection rule already recorded:', state['signal_rule_id'])
        return
    body = json.loads((ROOT/'templates/detection-rule.json').read_text())
    result, _ = api(state, 'POST', '/api/v2/security_monitoring/rules', body, app_key=True)
    rule_id = result.get('id') or result.get('data', {}).get('id')
    if not rule_id:
        save('signal-rule-response.json', result)
        fail('No rule ID returned; inspect generated/signal-rule-response.json before retrying.')
    state['signal_rule_id'] = rule_id
    save(state_filename, state)
    save('signal-rule-response.json', result)
    print('Detection rule created:', rule_id)
    print('EU1 signal query: @workflow.rule.id:' + rule_id)


def test(state, args):
    pod = ready_pod(state)
    current = pod['status']['containerStatuses'][0]['containerID'].split('://', 1)[1]
    if current != state.get('container_id'):
        fail('Container changed. Run python3 playground.py refresh-policy before testing.')
    repo = Path(args.repo).resolve()
    if not (repo/'assets/rce-malware/malware.x64').exists():
        fail('Repository assets not found. Pass --repo pointing to the cloned upstream repository.')
    base = ['kubectl', '--context', state['context'], '-n', APP_NS, 'exec', '-i', 'deployment/playground-app', '--']
    for dest, rel in [('playground-miner','assets/rce-malware/malware.x64'), ('playground-bpfdoor','assets/bpfdoor/fake-bpfdoor.x64')]:
        run(base+['sh','-c','cat > /tmp/'+dest+' && chmod 755 /tmp/'+dest], data=(repo/rel).read_bytes())
    print('Running', args.test, 'test(s). First matches can wait about 60 seconds for enforcement safeguards.', flush=True)
    code = (ROOT/'tests-in-container.py').read_text()
    result = subprocess.run(base+['python3','-','--test',args.test], input=code, text=True, capture_output=True)
    print(result.stdout)
    (OUT/'last-test-results.jsonl').write_text(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if result.returncode:
        fail('A test did not observe the expected outcome. Inspect generated/last-test-results.jsonl and the Datadog action status.')


def cleanup(state):
    owned=[]
    for ns in [APP_NS, AGENT_NS]:
        raw=kube(state,'get','namespace',ns,'--ignore-not-found','-o','json',capture=True).stdout
        if raw.strip():
            info=json.loads(raw)
            if info['metadata'].get('labels',{}).get('security-demo')!=OWNER:
                fail('Refusing cleanup because a namespace is not owned by this kit: '+ns)
            owned.append(ns)
    env = dict(os.environ, HELM_DRIVER='secret')
    run(['helm','uninstall',RELEASE,'--namespace',AGENT_NS,'--kube-context',state['context'],'--ignore-not-found'],env=env)
    for ns in owned:
        kube(state,'delete','namespace',ns)
    print('Kubernetes resources removed. Disable/delete the demo detection rule in the EU1 UI:',state.get('signal_rule_id','not created'))
    print('Retain generated/state.json for the rule ID. Remove generated/ before a fresh install.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('install',help='Install the scoped demo on one node; prompts for an EU1 API key')
    p.add_argument('--context',required=True);p.add_argument('--node',required=True);p.add_argument('--cluster-name',required=True)
    p.add_argument('--insecure-kubelet-tls',action='store_true',help='Only when approved for a kubelet certificate that cannot be validated')
    p=sub.add_parser('render',help='Render manifests and, optionally, a guarded policy without contacting Kubernetes or Datadog')
    p.add_argument('--node',required=True);p.add_argument('--cluster-name',required=True);p.add_argument('--context')
    p.add_argument('--container-id');p.add_argument('--existing-agent',action='store_true')
    for name in ['status','refresh-policy','cleanup']:
        sub.add_parser(name)
    p=sub.add_parser('create-signal-rule');p.add_argument('--api-only',action='store_true',help='For an existing-Agent/manual deployment; does not require install state')
    p=sub.add_parser('test');p.add_argument('--repo',required=True);p.add_argument('--test',choices=['miner','bpfdoor','mining-pool','all'],default='all')
    args=parser.parse_args()
    if args.command=='install':return install(args)
    if args.command=='render':
        if args.container_id and not re.fullmatch('[0-9a-f]{64}',args.container_id):
            fail('--container-id must be the complete 64-character container ID, without containerd://.')
        s={'node':args.node,'hostname_label':args.node,'cluster_name':args.cluster_name,'site':'datadoghq.eu'}
        resources=render(s)
        if args.existing_agent:
            resources['items']=[r for r in resources['items'] if not (r['kind']=='Namespace' and r['metadata']['name']==AGENT_NS)]
            save('workloads.json',resources)
        if args.container_id:
            data=json.loads((ROOT/'templates/playground.policy.json').read_text().replace('__PLAYGROUND_CONTAINER_ID__',args.container_id))
            save('playground.policy',data)
            if args.context:
                s.update({'context':args.context,'container_id':args.container_id})
                save('render-state.json',s)
        print('Rendered files in',OUT,'without cluster or Datadog changes.')
        return
    if args.command=='create-signal-rule' and args.api_only:
        p=OUT/'detection-state.json'
        s=json.loads(p.read_text()) if p.exists() else {'site':'datadoghq.eu'}
        return create_signal(s,'detection-state.json')
    if args.command=='test' and not STATE.exists() and (OUT/'render-state.json').exists():
        return test(json.loads((OUT/'render-state.json').read_text()),args)
    state=load_state()
    if args.command=='status':status(state)
    elif args.command=='create-signal-rule':create_signal(state)
    elif args.command=='test':test(state,args)
    elif args.command=='cleanup':cleanup(state)
    elif args.command=='refresh-policy':
        policy(state)
        kube(state,'-n',AGENT_NS,'rollout','restart','daemonset/'+RELEASE+'-datadog')
        kube(state,'-n',AGENT_NS,'rollout','status','daemonset/'+RELEASE+'-datadog','--timeout=180s')


if __name__=='__main__':
    try:
        main()
    except (subprocess.CalledProcessError,urllib.error.URLError) as e:
        fail(str(e))
