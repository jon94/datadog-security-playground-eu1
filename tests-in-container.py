"""Executed only inside the playground pod by playground.py."""
import argparse
import json
import os
import subprocess
import time

p=argparse.ArgumentParser();p.add_argument('--test',required=True);choice=p.parse_args().test
failed=False

def emit(result):
    print(json.dumps(result),flush=True)

def wait_kill(command, timeout=80):
    started=time.monotonic()
    proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    try:
        output,_=proc.communicate(timeout=timeout)
        return {'return_code':proc.returncode,'elapsed_seconds':round(time.monotonic()-started,3),'output':output}
    except subprocess.TimeoutExpired:
        proc.kill();proc.communicate()
        return {'return_code':'timeout; cleaned up by harness','elapsed_seconds':round(time.monotonic()-started,3)}

if choice in ['miner','all']:
    control=subprocess.Popen(['/tmp/playground-miner'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    time.sleep(1);alive=control.poll() is None
    control.terminate();control.wait(timeout=5)
    emit({'test':'control_without_mining_arguments','continued_running':alive})
    result=wait_kill(['/tmp/playground-miner','--cpu-priority','4'])
    result['test']='cryptominer_args';emit(result)
    failed |= not alive or result['return_code']!=-9

if choice in ['bpfdoor','all']:
    try:os.unlink('/var/run/haldrund.pid')
    except FileNotFoundError:pass
    proc=subprocess.Popen(['/tmp/playground-bpfdoor'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    output,_=proc.communicate(timeout=8)
    def alive_bpfdoor():
        found=[]
        for pid in os.listdir('/proc'):
            if not pid.isdigit():continue
            try:
                if os.readlink('/proc/'+pid+'/exe').removesuffix(' (deleted)')=='/tmp/playground-bpfdoor':found.append(int(pid))
            except (FileNotFoundError,PermissionError):pass
        return found
    end=time.monotonic()+80
    while alive_bpfdoor() and time.monotonic()<end:time.sleep(.25)
    remaining=alive_bpfdoor()
    emit({'test':'bpfdoor','daemon_parent_return_code':proc.returncode,'remaining_pids':remaining,'output':output,'verification':'Require status:performed for cws_playground_block_bpfdoor_bpf_filter_creation in Datadog; parent exit alone is not proof.'})
    if remaining:
        for pid in remaining:
            try:os.kill(pid,9)
            except ProcessLookupError:pass
        failed=True

if choice in ['mining-pool','all']:
    child="import socket,time\ntry:\n s=socket.create_connection(('donate.v2.xmrig.com',3333),timeout=4)\nexcept Exception as e:\n print(type(e).__name__,flush=True)\ntime.sleep(75)"
    result=wait_kill(['python3','-c',child],timeout=85)
    result.update({'test':'mining_pool_domain_v2','protocol_payload_sent':False});emit(result)
    failed |= result['return_code']!=-9

if failed:raise SystemExit(1)
