"""HTTP-only client for the Giraffe internal decision-card API; no daemon or secrets in output."""
import argparse, json, os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

def call(method,path,payload=None):
    base=os.getenv('GIRAFFE_URL','http://127.0.0.1:8000').rstrip('/'); key=os.getenv('INTERNAL_API_KEY','')
    if not key: raise SystemExit('INTERNAL_API_KEY is required')
    body=None if payload is None else json.dumps(payload,ensure_ascii=False).encode()
    req=Request(base+path,data=body,method=method,headers={'X-Internal-API-Key':key,'Content-Type':'application/json'})
    with urlopen(req,timeout=20) as r:return json.load(r)
def main(argv=None):
    p=argparse.ArgumentParser(prog='python -m kr_stock_autotrader.cli'); s=p.add_subparsers(dest='cmd',required=True)
    a=s.add_parser('today-evidence');a.add_argument('--date',required=True)
    for n in ('pending-cards',): s.add_parser(n)
    for n,arg in (('evidence-detail','evidence_id'),('filter-detail','filter_id'),('card-detail','card_id')): a=s.add_parser(n);a.add_argument(arg)
    for n in ('evidence-add','evidence-update','filter-run','card-request','card-save-result'): a=s.add_parser(n);a.add_argument('json')
    a=s.add_parser('evidence-invalidate');a.add_argument('evidence_id')
    a=s.add_parser('scheduler-start');a.add_argument('run_key');a.add_argument('kind')
    a=s.add_parser('scheduler-finish');a.add_argument('run_key');a.add_argument('status');a.add_argument('--count',type=int,default=0);a.add_argument('--detail',default='{}')
    x=p.parse_args(argv)
    if x.cmd=='today-evidence': out=call('GET','/api/internal/evidence?'+urlencode({'date':x.date}))
    elif x.cmd=='evidence-detail': out=call('GET','/api/internal/evidence/'+x.evidence_id)
    elif x.cmd=='filter-detail': out=call('GET','/api/internal/filters/'+x.filter_id)
    elif x.cmd=='card-detail': out=call('GET','/api/internal/cards/'+x.card_id)
    elif x.cmd=='pending-cards': out=call('GET','/api/internal/cards')
    elif x.cmd=='evidence-add': out=call('POST','/api/internal/evidence',json.loads(x.json))
    elif x.cmd=='evidence-update':
        payload=json.loads(x.json); out=call('PATCH','/api/internal/evidence/'+str(payload.pop('id')),payload)
    elif x.cmd=='evidence-invalidate': out=call('POST','/api/internal/evidence/'+x.evidence_id+'/invalidate',{})
    elif x.cmd=='filter-run': out=call('POST','/api/internal/filters',json.loads(x.json))
    elif x.cmd=='card-request': out=call('POST','/api/internal/cards/generate',json.loads(x.json))
    elif x.cmd=='card-save-result': out=call('POST','/api/internal/cards/results',json.loads(x.json))
    elif x.cmd=='scheduler-start': out=call('POST',f'/api/internal/scheduler-runs/{x.run_key}/start',{'kind':x.kind})
    else: out=call('POST',f'/api/internal/scheduler-runs/{x.run_key}/finish',{'status':x.status,'count':x.count,'detail':json.loads(x.detail)})
    print(json.dumps(out,ensure_ascii=False,sort_keys=True));return 0
if __name__=='__main__': raise SystemExit(main())
