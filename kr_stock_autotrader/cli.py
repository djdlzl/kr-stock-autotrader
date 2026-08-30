"""Small HTTP client for the Giraffe internal decision-card API; no daemon."""
import argparse, json, os, sys
from urllib.request import Request, urlopen

def call(method,path,payload=None):
    base=os.getenv('GIRAFFE_URL','http://127.0.0.1:8000').rstrip('/'); key=os.getenv('INTERNAL_API_KEY','')
    if not key: raise SystemExit('INTERNAL_API_KEY is required')
    body=None if payload is None else json.dumps(payload,ensure_ascii=False).encode()
    req=Request(base+path,data=body,method=method,headers={'X-Internal-API-Key':key,'Content-Type':'application/json'})
    with urlopen(req,timeout=20) as r:return json.load(r)
def main(argv=None):
 p=argparse.ArgumentParser(prog='python -m kr_stock_autotrader.cli'); s=p.add_subparsers(dest='cmd',required=True)
 for n in ('today-evidence','pending-cards'): s.add_parser(n)
 a=s.add_parser('evidence-detail');a.add_argument('evidence_id')
 a=s.add_parser('evidence-add');a.add_argument('json')
 a=s.add_parser('card-request');a.add_argument('json')
 a=s.add_parser('card-save-result');a.add_argument('json')
 a=s.add_parser('filter-run');a.add_argument('json')
 a=s.add_parser('scheduler-start');a.add_argument('run_key');a.add_argument('kind')
 a=s.add_parser('scheduler-finish');a.add_argument('run_key');a.add_argument('status')
 x=p.parse_args(argv)
 if x.cmd=='today-evidence': out=call('GET','/api/internal/evidence')
 elif x.cmd=='evidence-detail': out=call('GET','/api/internal/evidence/'+x.evidence_id)
 elif x.cmd=='pending-cards': out=call('GET','/api/internal/cards')
 elif x.cmd=='evidence-add': out=call('POST','/api/internal/evidence',json.loads(x.json))
 elif x.cmd=='filter-run': out=call('POST','/api/internal/filters',json.loads(x.json))
 elif x.cmd=='card-request': out=call('POST','/api/internal/cards/generate',json.loads(x.json))
 elif x.cmd=='card-save-result': out=call('POST','/api/internal/cards/results',json.loads(x.json))
 elif x.cmd=='scheduler-start': out=call('POST',f'/api/internal/scheduler-runs/{x.run_key}/start',{'kind':x.kind})
 else: out=call('POST',f'/api/internal/scheduler-runs/{x.run_key}/finish',{'status':x.status})
 print(json.dumps(out,ensure_ascii=False,sort_keys=True));return 0
if __name__=='__main__': raise SystemExit(main())
