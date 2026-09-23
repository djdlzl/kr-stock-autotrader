"""HTTP-only client for the Giraffe internal decision-card API; no daemon or secrets in output."""
import argparse, json, os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

def call(method,path,payload=None,*,research_control=False):
    base=os.getenv('GIRAFFE_URL','http://127.0.0.1:8000').rstrip('/'); key=os.getenv('RESEARCH_CONTROL_KEY' if research_control else 'INTERNAL_API_KEY','')
    if not key: raise SystemExit('RESEARCH_CONTROL_KEY is required' if research_control else 'INTERNAL_API_KEY is required')
    body=None if payload is None else json.dumps(payload,ensure_ascii=False).encode()
    header='X-Research-Control-Key' if research_control else 'X-Internal-API-Key'
    req=Request(base+path,data=body,method=method,headers={header:key,'Content-Type':'application/json'})
    with urlopen(req,timeout=20) as r:return json.load(r)
def main(argv=None):
    p=argparse.ArgumentParser(prog='python -m kr_stock_autotrader.cli'); s=p.add_subparsers(dest='cmd',required=True)
    a=s.add_parser('today-evidence');a.add_argument('--date',required=True)
    for n in ('pending-cards',): s.add_parser(n)
    for n,arg in (('evidence-detail','evidence_id'),('filter-detail','filter_id'),('card-detail','card_id')): a=s.add_parser(n);a.add_argument(arg)
    a=s.add_parser('filter-head');a.add_argument('evidence_id');a.add_argument('as_of');a.add_argument('known_at')
    for n in ('evidence-add','evidence-update','filter-run','card-request','card-save-result'): a=s.add_parser(n);a.add_argument('json')
    a=s.add_parser('evidence-invalidate');a.add_argument('evidence_id')
    a=s.add_parser('market-snapshot');a.add_argument('symbol');a.add_argument('as_of');a.add_argument('--announcement-at');a.add_argument('--premarket-retry', action='store_true')
    a=s.add_parser('market-context');a.add_argument('card_id');a.add_argument('run_key');a.add_argument('as_of')
    a=s.add_parser('scheduler-start');a.add_argument('run_key');a.add_argument('kind')
    a=s.add_parser('scheduler-finish');a.add_argument('run_key');a.add_argument('status');a.add_argument('--count',type=int,default=0);a.add_argument('--detail',default='{}')
    a=s.add_parser('scheduler-latest');a.add_argument('kind');a.add_argument('--date',required=True)
    a=s.add_parser('scheduler-readback');a.add_argument('run_key')
    a=s.add_parser('dart-terminal-plan');a.add_argument('json')
    a=s.add_parser('dart-terminal-batch');a.add_argument('json')
    a=s.add_parser('giraffe-review-queue');a.add_argument('run_key');a.add_argument('control_contract_sha256');a.add_argument('--offset',type=int,default=0);a.add_argument('--limit',type=int,default=25)
    a=s.add_parser('giraffe-review-manifest');a.add_argument('run_key');a.add_argument('control_contract_sha256');a.add_argument('--page-size',type=int,default=25)
    a=s.add_parser('giraffe-review-packet');a.add_argument('run_key');a.add_argument('control_contract_sha256');a.add_argument('rcp_no')
    a=s.add_parser('dart-terminal-batch-handle');a.add_argument('run_key');a.add_argument('control_contract_sha256');a.add_argument('audits_json')
    x=p.parse_args(argv)
    if x.cmd=='giraffe-review-queue':
        from kr_stock_autotrader.giraffe_review_queue import compact_review_queue
        out=compact_review_queue(x.run_key,x.control_contract_sha256,offset=x.offset,limit=x.limit)
    elif x.cmd=='giraffe-review-manifest':
        from kr_stock_autotrader.giraffe_review_queue import compact_review_manifest
        out=compact_review_manifest(x.run_key,x.control_contract_sha256,page_size=x.page_size)
    elif x.cmd=='giraffe-review-packet':
        from kr_stock_autotrader.giraffe_review_queue import open_review_packet
        out=open_review_packet(x.run_key,x.control_contract_sha256,x.rcp_no)
    elif x.cmd=='dart-terminal-batch-handle':
        from kr_stock_autotrader.giraffe_review_queue import terminal_batch_handle
        out=terminal_batch_handle(x.run_key,x.control_contract_sha256,json.loads(x.audits_json))
    elif x.cmd=='dart-terminal-plan':
        from kr_stock_autotrader.giraffe_terminal_audit import terminal_audit_plan
        payload=json.loads(x.json); out=terminal_audit_plan(payload.get('source'),payload.get('audit'))
    elif x.cmd=='dart-terminal-batch':
        from kr_stock_autotrader.giraffe_terminal_audit import terminal_audit_batch
        payload=json.loads(x.json); out=terminal_audit_batch(payload.get('sources'), payload.get('audits'))
    elif x.cmd=='today-evidence': out=call('GET','/api/internal/evidence?'+urlencode({'date':x.date}))
    elif x.cmd=='evidence-detail': out=call('GET','/api/internal/evidence/'+x.evidence_id)
    elif x.cmd=='filter-detail': out=call('GET','/api/internal/filters/'+x.filter_id)
    elif x.cmd=='filter-head': out=call('GET','/api/internal/filters/head?'+urlencode({'evidence_id':x.evidence_id,'as_of':x.as_of,'known_at':x.known_at}))
    elif x.cmd=='card-detail': out=call('GET','/api/internal/cards/'+x.card_id)
    elif x.cmd=='pending-cards': out=call('GET','/api/internal/cards?missing=true')
    elif x.cmd=='evidence-add': out=call('POST','/api/internal/evidence',json.loads(x.json))
    elif x.cmd=='evidence-update':
        payload=json.loads(x.json); out=call('PATCH','/api/internal/evidence/'+str(payload.pop('id')),payload)
    elif x.cmd=='evidence-invalidate': out=call('POST','/api/internal/evidence/'+x.evidence_id+'/invalidate',{})
    elif x.cmd=='market-snapshot': out=call('POST','/api/internal/market-snapshots/'+x.symbol,{'as_of':x.as_of, **({'announcement_at':x.announcement_at} if x.announcement_at else {}), **({'premarket_retry': True} if x.premarket_retry else {})})
    elif x.cmd=='filter-run': out=call('POST','/api/internal/filters',json.loads(x.json))
    elif x.cmd=='card-request': out=call('POST','/api/internal/cards/generate',json.loads(x.json))
    elif x.cmd=='card-save-result': out=call('POST','/api/internal/cards/results',json.loads(x.json))
    elif x.cmd=='scheduler-start':
        out=call('POST',f'/api/internal/scheduler-runs/{x.run_key}/start',{'kind':x.kind})
    elif x.cmd=='scheduler-latest': out=call('GET','/api/internal/scheduler-runs/latest?'+urlencode({'kind':x.kind,'date':x.date}))
    elif x.cmd=='scheduler-readback': out=call('GET',f'/api/internal/scheduler-runs/{x.run_key}',research_control=True)
    elif x.cmd=='market-context': out=call('POST',f'/api/internal/cards/{x.card_id}/market-context',{'run_key':x.run_key,'as_of':x.as_of})
    else: out=call('POST',f'/api/internal/scheduler-runs/{x.run_key}/finish',{'status':x.status,'count':x.count,'detail':json.loads(x.detail)})
    print(json.dumps(out,ensure_ascii=False,sort_keys=True));return 0
if __name__=='__main__': raise SystemExit(main())
