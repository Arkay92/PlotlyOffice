import json
import math
import random
import pandas as pd
import plotly.express as px
from plotly.utils import PlotlyJSONEncoder
from .data import blend

PALETTES = {'lagoon':['#14857b','#72b5a4','#edb766','#516e9e','#c788a2'], 'iris':['#7262bb','#ab9cda','#dfab64','#568f9a','#cd7887'], 'ember':['#bd7250','#e5b37c','#82956a','#5f8099','#b593b5']}
KINDS = {'line','area','bar','scatter','donut','histogram','table'}

def validate(plan, tables):
    if not isinstance(plan, dict): raise ValueError('The model did not return a dashboard object.')
    resolved, lineage = blend(tables, plan.get('joins', []))
    charts = plan.get('charts', [])
    if not isinstance(charts, list) or not 1 <= len(charts) <= 8:
        raise ValueError('A dashboard needs 1–8 charts.')
    for chart in charts:
        df = resolved[chart['source']]
        if chart.get('kind') not in KINDS: raise ValueError('Unsupported chart type.')
        for field in ('x','y','color'):
            if chart.get(field) and chart[field] not in df: raise ValueError(f'Unknown column: {chart[field]}')
        kind = chart['kind']
        if kind != 'table' and not chart.get('x'): raise ValueError('Chart needs an x column.')
        if kind in {'line','area','bar','scatter','donut'} and not chart.get('y'): raise ValueError('Chart needs a y column.')
        if chart.get('y') and not pd.api.types.is_numeric_dtype(df[chart['y']]) and (chart.get('aggregation') != 'count' or kind == 'scatter'): raise ValueError('Chart measures must be numeric unless counting records.')
        if chart.get('aggregation', 'sum') not in {'sum','mean','count','min','max','none'}: raise ValueError('Unsupported aggregation.')
        if kind not in {'table','histogram','scatter'} and chart.get('y') in {chart.get('x'),chart.get('color')}: raise ValueError('Group and measure must be different columns.')
        if kind == 'donut' and chart.get('aggregation') != 'count' and (df[chart['y']].dropna()<0).any(): raise ValueError('Donut charts require non-negative measures.')
        chart['span'] = 2 if chart.get('span') == 2 else 1
    metrics = plan.get('metrics', [])
    if not isinstance(metrics, list) or len(metrics) > 4: raise ValueError('Use up to four metrics.')
    for m in metrics:
        df = resolved[m['source']]
        if m.get('aggregation') not in {'sum','mean','count','nunique','min','max'}: raise ValueError('Unsupported metric aggregation.')
        if m.get('column') not in df: raise ValueError('Unknown metric column.')
        if m['aggregation'] not in {'count','nunique'} and not pd.api.types.is_numeric_dtype(df[m['column']]): raise ValueError('Metric must be numeric.')
    filters = plan.get('filters', [])
    if not isinstance(filters, list) or len(filters) > 4: raise ValueError('Use up to four filters.')
    for f in filters:
        if f['column'] not in resolved[f['source']]: raise ValueError('Unknown filter column.')
    if not isinstance(plan.get('notes',[]),list) or any(not isinstance(n,str) for n in plan.get('notes',[])): raise ValueError('Dashboard notes must be a list of text strings.')
    plan['palette'] = plan.get('palette') if plan.get('palette') in PALETTES else 'lagoon'
    return plan, resolved, lineage

def render(plan, tables, selections=None):
    plan, resolved, lineage = validate(plan, tables)
    selections = selections or {}
    filters = []
    for i, f in enumerate(plan.get('filters', [])):
        df = resolved[f['source']]
        options = sorted(df[f['column']].dropna().astype(str).unique().tolist())[:200]
        filters.append({**f, 'id':str(i), 'options':options})
    # Explicit lineage maps prevent a same-named but unrelated column being filtered.
    origins = {name:{column:{(name,column)} for column in df} for name,df in tables.items()}
    for j in plan.get('joins', []):
        left, right, alias = j['left'],j['right'],j['name']
        mapping = {c:set(v) for c,v in origins[left].items()}
        for col, lineage_origins in origins[right].items():
            if col == j['right_on'] and col == j['left_on']:
                mapping[col] |= lineage_origins
            else:
                target = col+'__'+alias if col in mapping else col
                mapping[target] = set(lineage_origins)
        origins[alias] = mapping
    # Filters propagate through shared lineage, including chained blends.
    for name, df in resolved.items():
        for i,f in enumerate(plan.get('filters', [])):
            value = selections.get(str(i))
            if not value: continue
            filter_origins = origins[f['source']][f['column']]
            for column, column_origins in origins[name].items():
                if filter_origins & column_origins:
                    df = df[df[column].astype(str) == value]
        resolved[name] = df
    metrics = []
    for m in plan.get('metrics', []):
        series = resolved[m['source']][m['column']]
        value = getattr(series, m['aggregation'])()
        value = float(value) if pd.notna(value) else None
        metrics.append({**m, 'value':value})
    charts = []
    colors = PALETTES[plan['palette']]
    for index, c in enumerate(plan['charts']):
        df = resolved[c['source']]
        kind, x, y, color = c['kind'], c.get('x'), c.get('y'), c.get('color')
        if kind == 'table':
            charts.append({**c, 'columns':list(df.columns)[:12], 'rows':json.loads(df.iloc[:50,:12].to_json(orient='records')), 'total_rows':len(df)})
            continue
        aggregation = c.get('aggregation','sum')
        if kind not in {'scatter','histogram'} and aggregation != 'none':
            keys = list(dict.fromkeys(k for k in [x,color] if k))
            if y in keys: raise ValueError('Group and measure must be different columns.')
            df = df.groupby(keys, dropna=False, as_index=False)[y].agg(aggregation)
        if kind in {'line','area'}: df = df.sort_values(x)
        if kind in {'bar','donut'} and y: df = df.sort_values(y, ascending=False).head(20)
        if kind == 'donut' and not df.empty and (df[y] < 0).any(): raise ValueError('Donut charts require non-negative values.')
        total = len(df)
        if total > 2000: df = df.iloc[::math.ceil(total / 2000)]
        kwargs = dict(data_frame=df, color_discrete_sequence=colors[index % len(colors):]+colors[:index % len(colors)])
        if kind == 'donut': fig = px.pie(**kwargs, names=x, values=y, hole=.72)
        elif kind == 'histogram': fig = px.histogram(**kwargs, x=x, color=color, nbins=30)
        else: fig = getattr(px, kind)(**kwargs, x=x, y=y, color=color)
        fig.update_layout(template='plotly_white', height=310, margin=dict(l=40,r=18,t=20,b=45), paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(family='Arial, sans-serif',color='#59645f',size=11), legend=dict(orientation='h',y=1.14,x=0), uirevision=json.dumps(c,sort_keys=True))
        fig.update_xaxes(showgrid=False, title=None)
        fig.update_yaxes(gridcolor='#edf0eb', title=None, zeroline=False)
        charts.append({**c, 'figure':json.loads(json.dumps(fig.to_plotly_json(), cls=PlotlyJSONEncoder)), 'sampled':total > 2000})
    return {'title':plan.get('title','Your dashboard'), 'subtitle':plan.get('subtitle',''), 'palette':plan['palette'], 'metrics':metrics,'charts':charts,'filters':filters,'lineage':lineage,'notes':plan.get('notes',[])}

def demo_plan():
    return {'title':'A little clarity. A bigger picture.', 'subtitle':'A connected view of revenue, customers and the places you’re growing.', 'palette':'lagoon', 'joins':[{'left':'orders','right':'customers','left_on':'customer_id','right_on':'customer_id','name':'sales'}], 'metrics':[{'label':'Total revenue','source':'sales','column':'revenue','aggregation':'sum','format':'currency','currency':'GBP'}, {'label':'Orders','source':'sales','column':'order_id','aggregation':'count'}, {'label':'Average order','source':'sales','column':'revenue','aggregation':'mean','format':'currency','currency':'GBP'}, {'label':'Customers reached','source':'sales','column':'customer_id','aggregation':'nunique'}], 'charts':[{'title':'Revenue over time','source':'sales','kind':'area','x':'date','y':'revenue','aggregation':'sum'}, {'title':'Revenue by channel','source':'sales','kind':'donut','x':'channel','y':'revenue','aggregation':'sum'}, {'title':'Where growth happens','source':'sales','kind':'bar','x':'region','y':'revenue','aggregation':'sum'}, {'title':'Your best-selling categories','source':'sales','kind':'bar','x':'category','y':'revenue','color':'category','aggregation':'sum'}], 'filters':[{'source':'sales','column':'region'},{'source':'sales','column':'channel'}], 'notes':['Sample workspace · synthetic GBP sales data, not an AI-generated report.','Orders are linked to customers using a unique customer_id. Revenue stays at order level.']}

def demo_tables():
    rng = random.Random(42)
    customers = pd.DataFrame([{'customer_id':f'C{i:03}','region':['Europe','North America','Asia Pacific','Middle East'][i%4], 'segment':['Consumer','Business'][i%2]} for i in range(120)])
    orders = pd.DataFrame([{'order_id':i+1,'customer_id':f'C{rng.randrange(120):03}','date':str(pd.Timestamp('2026-01-01').date()+pd.Timedelta(days=i//8)), 'category':rng.choice(['Home & living','Apparel','Accessories','Wellness']), 'channel':rng.choice(['Online store','Retail','Partners']), 'revenue':round(rng.uniform(48,420)*(1+i/5000),2)} for i in range(1440)])
    return {'orders':orders,'customers':customers}
