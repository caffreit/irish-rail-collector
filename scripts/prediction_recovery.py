"""Reproducible prediction scoring and as-of delay recovery from saved trial bodies."""
import argparse,bisect,collections,contextlib,datetime as dt,gzip,html,io,json,statistics,sys,urllib.parse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.audit_coverage import audit,expected,epoch,around,passenger,TZ
from dart_trial import parse_rows,service_key


def forecast_at(snapshots,station,actual,horizon,scheduled,max_age=180):
    cutoff=actual-horizon*60
    candidates=[s for s in snapshots if s['finished']<=cutoff]
    if not candidates:return None
    snap=max(candidates,key=lambda s:s['finished'])
    if cutoff-snap['finished']>max_age:return None
    row=next((r for r in passenger(snap['rows']) if r['locationcode'].strip()==station and r['locationtype']!='O'),None)
    if not row or row.get('arrival') or not row.get('expectedarrival') or row['expectedarrival']=='00:00:00':return None
    if not row.get('scheduledarrival') or around(row['scheduledarrival'],scheduled)!=scheduled:return None
    predicted=around(row['expectedarrival'],scheduled)
    return {'error_seconds':predicted-actual,'baseline_error_seconds':scheduled-actual,'fetched':snap['finished'],'lead_minutes':(actual-snap['finished'])/60,'raw_key':snap['raw_key']}


def recovered_after(polls,peak_t):
    first=None;last=None
    for p in polls:
        if p['t']<peak_t:continue
        eligible=p['active']>=5 and p['covered']/p['active']>=.8
        good=eligible and (p['covered']-p['late'])/p['covered']>=.8
        if not good:first=None
        elif first is None or (last is not None and p['t']-last>300):first=p['t']
        if good and p['t']-first>=1800:return {'began':first,'confirmed':p['t'],'minutes_from_peak':(first-peak_t)/60}
        last=p['t']
    return None


def analyse(directory):
    with contextlib.redirect_stdout(io.StringIO()):coverage,evidence,indexes,polls,_=audit(directory)
    core=[t for t in coverage['journeys'] if t['cohort']=='departures_in_window' and t['status']=='strict_match']
    index=next(iter(indexes.values()));plans={}
    for date in sorted({t['service_date'] for t in core}):
        for t in expected(index,dt.date.fromisoformat(date)):plans[(t['code'],t['service_date'])]=t
    scored=[];excluded=collections.Counter();ground_events=0
    for t in core:
        key=tuple(t['observed_key']);ev=evidence[key];trip=plans[(t['code'],t['service_date'])]
        for station,stop in zip(trip['pattern'][1:],trip['stops'][1:]):
            actual=ev['actuals'].get(station+'|arrival')
            if not actual:excluded['missing_final_actual']+=1;continue
            if actual['midnight_review']:excluded['midnight_value_review']+=1;continue
            scheduled=epoch(t['service_date'],stop['arrival_time']);a=around(actual['value'],scheduled)
            if a>actual['finished']+120 or abs(a-scheduled)>10800:excluded['implausible_actual']+=1;continue
            ground_events+=1
            for horizon in [5,10,20]:
                f=forecast_at(ev['snapshots'],station,a,horizon,scheduled)
                if f:scored.append({'service':key[0]+'|'+key[1],'station':station,'horizon':horizon,'actual':a,'actual_raw_key':actual['raw_key'],**f})
                else:excluded[str(horizon)+'min_no_eligible_forecast']+=1
    summaries=[]
    for horizon in [5,10,20]:
        rows=[s for s in scored if s['horizon']==horizon];errors=[s['error_seconds'] for s in rows];absolute=[abs(s) for s in errors]
        summaries.append({'horizon':horizon,'n':len(rows),'journeys':len({r['service'] for r in rows}),'mean_absolute_seconds':statistics.mean(absolute) if absolute else None,'median_absolute_seconds':statistics.median(absolute) if absolute else None,'within_60_percent':100*sum(a<=60 for a in absolute)/len(rows) if rows else None,'mean_signed_seconds':statistics.mean(errors) if rows else None,'baseline_mean_absolute_seconds':statistics.mean(abs(r['baseline_error_seconds']) for r in rows) if rows else None})
    # Running cohorts come from the actual position response. Movement forecasts
    # must have been fetched already and cannot be newer than that response.
    timelines={}
    for key,e in evidence.items():
        vals=[]
        for snap in sorted(e['snapshots'],key=lambda s:s['finished']):
            delay=None
            for typ,field in [('N','arrival'),('C','departure')]:
                candidates=sorted((r for r in snap['rows'] if r.get('stoptype')==typ and r.get('locationtype')!='T'),key=lambda r:int(r['locationorder']))
                for r in candidates:
                    sched=r.get('scheduled'+field);pred=r.get('expected'+field)
                    if sched and pred and pred!='00:00:00':
                        base=around(sched,snap['finished']);delay=(around(pred,base)-base)/60;break
                if delay is not None:break
            vals.append((snap['finished'],delay))
        timelines[key]=([v[0] for v in vals],vals)
    ids={tuple(t['observed_key']) for t in coverage['journeys'] if t['status']=='strict_match'};series=[]
    for p in polls:
        for req in p['requests']:
            if req['source']!='irishrail' or 'getCurrentTrainsXML' not in req['url'] or not req.get('raw_key') or req.get('error'):continue
            rows=parse_rows(gzip.decompress((directory/'objects'/req['raw_key']).read_bytes()));active=0;values=[];now=req['finished']
            for r in rows:
                key=service_key(r)
                if key not in ids or r.get('trainstatus')!='R':continue
                active+=1;times,vals=timelines.get(key,([],[]));pos=bisect.bisect_right(times,now)-1
                if pos>=0 and now-times[pos]<=300 and vals[pos][1] is not None:values.append(vals[pos][1])
            series.append({'t':now,'active':active,'covered':len(values),'late':sum(v>5 for v in values),'total':sum(max(0,v) for v in values)})
    eligible=[p for p in series if p['active']>=5 and p['covered']/p['active']>=.8 and dt.datetime.fromtimestamp(p['t'],TZ).date()==dt.date(2026,11,22) and 6<=dt.datetime.fromtimestamp(p['t'],TZ).hour<22]
    peak=max(eligible,key=lambda p:p['total']) if eligible else None
    recovery=recovered_after(series,peak['t']) if peak else None
    out={'window':coverage['window'],'ground_truth_arrivals':ground_events,'scored':scored,'summary':summaries,'excluded':dict(excluded),'polls':series,'peak':peak,'recovery':recovery,'scope':'89 timetable-matched departures in original 21–22 November AWS trial. Original automatic reported times are the reference; no independent physical ground truth and no later backfill.'}
    (directory/'prediction-recovery.json').write_text(json.dumps(out,indent=2));return out


def render(data,path):
    from scripts.render_coverage import shell
    def time(t):return dt.datetime.fromtimestamp(t,TZ).strftime('%d Nov %H:%M')
    rows=''.join('<tr>'+''.join('<td>'+str(v)+'</td>' for v in [r['horizon'],r['n'],r['journeys'],round(r['mean_absolute_seconds'],1) if r['n'] else 'n/a',round(r['baseline_mean_absolute_seconds'],1) if r['n'] else 'n/a',str(round(r['within_60_percent'],1))+'%' if r['n'] else 'n/a'])+'</tr>' for r in data['summary'])
    body='<h1>How useful are the arrival predictions?</h1><p>Irish Rail DART archive, 21–22 November 2026. Each stop is scored once per forecast horizon against its final reported arrival. Automatic source times are the reference, so this measures agreement with those times.</p><table><thead><tr><th>Minutes before arrival</th><th>Scored stops</th><th>Journeys</th><th>Prediction error, mean seconds</th><th>Timetable error, mean seconds</th><th>Within 1 minute</th></tr></thead><tbody>'+rows+'</tbody></table>'
    body+='<figure><svg id="accuracy" viewBox="0 0 700 230" aria-label="Mean absolute arrival prediction error by horizon"></svg><figcaption>Lower is better. Prediction and timetable use the same stops at each horizon. The eligible stops differ between horizons; this is not a controlled comparison of an identical cohort.</figcaption></figure>'
    body+='<details><summary>Scoring rules and missing data</summary><p>Use the most recent movement snapshot fetched at or before 5, 10 or 20 minutes before the reported arrival. It must be no more than three minutes old at that cutoff, and its actual-arrival field must still be empty. The arrival schedule must match the archived GTFS. This excludes forecasts fetched after the cutoff, already reported arrivals, missing records and suspicious midnight values. Stops on a journey are correlated; these are descriptive statistics from one day, without confidence intervals.</p><pre>'+html.escape(json.dumps(data['excluded'],indent=2))+'</pre><p>'+str(data['ground_truth_arrivals'])+' valid reported arrivals were available before forecast-availability filtering. Forecast horizon is defined retrospectively relative to the final reported arrival. Scheduled arrival is the baseline forecast.</p></details>'
    peak=data['peak'];recovery=data['recovery']
    body+='<h2>When does the network settle after a delay peak?</h2>'
    if peak:
        body+=f'<p>The largest well-covered daytime forecast-delay peak on 22 November was at <b>{time(peak["t"])}</b>: {peak["total"]:.1f} positive train-minutes across {peak["covered"]} of {peak["active"]} running trains; {peak["late"]} were more than five minutes late.</p>'
        body+=f'<p>The recovery proxy first began at {time(recovery["began"])}, {recovery["minutes_from_peak"]:.0f} minutes after the peak, and was confirmed at {time(recovery["confirmed"])}.</p>' if recovery else '<p>No sustained recovery meeting the rule was established before the observations ended.</p>'
    body+='<label>Plot <select id="measure"><option value="total">Positive forecast delay, train-minutes</option><option value="late">Running trains more than 5 minutes late</option><option value="covered">Running trains with a fresh forecast</option></select></label><figure><svg id="network" viewBox="0 0 700 260" aria-label="Network delay through the day"></svg><figcaption>22 November, 06:00–22:00 Dublin time. Gaps indicate fewer than five running trains or forecasts covering less than 80% of them. Hover over a point for counts.</figcaption></figure>'
    body+='<p class="callout">Recovery means at least 80% of trains with a fresh forecast are no more than five minutes late, continuously for at least 30 minutes. Each sample needs five running trains and 80% forecast coverage; gaps longer than five minutes reset the clock. The train cohort changes. This describes network conditions, without establishing a physical traffic jam or its cause.</p><p class="note">A train’s delay comes from its next arrival forecast, or its current departure forecast when no next arrival is present. Only snapshots already fetched at each position report are used, with a maximum age of five minutes. Source fetch times can differ from source measurement times. Missing trains remain visible in coverage counts and never become zero-delay trains.</p>'
    display={k:data[k] for k in ['summary','polls','peak','recovery']}
    body+='''<script>const d='''+json.dumps(display)+''';const NS='http://www.w3.org/2000/svg';function el(tag,a,text){let n=document.createElementNS(NS,tag);for(let k in a)n.setAttribute(k,a[k]);if(text!==undefined)n.textContent=text;return n}const acc=document.querySelector('#accuracy'),maximum=Math.max(1,...d.summary.flatMap(r=>[r.mean_absolute_seconds||0,r.baseline_mean_absolute_seconds||0]));d.summary.forEach((r,i)=>{let y=15+i*65;acc.append(el('text',{x:0,y:y+18},r.horizon+' minutes'));[['mean_absolute_seconds','var(--chart-1,#5a9)'],['baseline_mean_absolute_seconds','var(--chart-2,#c96)']].forEach(([key,color],j)=>{let value=r[key]||0;acc.append(el('rect',{x:110,y:y+j*23,width:value/maximum*450,height:18,fill:color}),el('text',{x:120+value/maximum*450,y:y+j*23+14},value.toFixed(1)+'s'))})});acc.append(el('text',{x:110,y:225},'First bar: prediction. Second bar: timetable. Mean absolute error.'));const svg=document.querySelector('#network'),series=d.polls.filter(p=>new Date(p.t*1000).getUTCDate()===22&&new Date(p.t*1000).getUTCHours()>=6&&new Date(p.t*1000).getUTCHours()<22);function plot(){svg.replaceChildren();if(!series.length)return;const key=document.querySelector('#measure').value,left=Date.UTC(2026,10,22,6)/1000,right=Date.UTC(2026,10,22,22)/1000,max=Math.max(1,...series.map(p=>p[key])),x=t=>45+(t-left)/(right-left)*630,y=v=>220-v/max*180;[0,max/2,max].forEach(v=>svg.append(el('line',{x1:45,y1:y(v),x2:680,y2:y(v),stroke:'var(--border,#555)'}),el('text',{x:0,y:y(v)+4},v.toFixed(0))));for(let h=6;h<=22;h+=2)svg.append(el('text',{x:x(Date.UTC(2026,10,22,h)/1000)-12,y:245},h+':00'));let previous=null;for(let p of series){if(p.active<5||p.covered/p.active<.8){previous=null;continue}if(previous&&p.t-previous.t<=300)svg.append(el('line',{x1:x(previous.t),y1:y(previous[key]),x2:x(p.t),y2:y(p[key]),stroke:'var(--chart-1,#5a9)','stroke-width':2}));let dot=el('circle',{cx:x(p.t),cy:y(p[key]),r:2,fill:'var(--chart-1,#5a9)'});dot.append(el('title',{},new Date(p.t*1000).toISOString().slice(11,16)+': '+p[key].toFixed(1)+'; '+p.covered+'/'+p.active+' covered; '+p.late+' >5 min late'));svg.append(dot);previous=p}if(d.peak)svg.append(el('line',{x1:x(d.peak.t),x2:x(d.peak.t),y1:30,y2:220,stroke:'var(--chart-2,#c96)','stroke-dasharray':'4 4'}));}document.querySelector('#measure').onchange=plot;plot();</script>'''
    page=shell('DART prediction accuracy and delay recovery',body).replace('<style>','<style>:root{color-scheme:light dark}').replace('var(--foreground,#ddd)','var(--foreground,CanvasText)').replace('var(--muted-foreground,#999)','var(--muted-foreground,GrayText)')
    path.write_text(page)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,default=Path('data/coverage-audit'));args=p.parse_args();data=analyse(args.directory);render(data,args.directory/'prediction-recovery.html');print(json.dumps({k:data[k] for k in ['summary','excluded','peak','recovery']},indent=2))
