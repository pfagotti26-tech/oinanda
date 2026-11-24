"""Leads do site: quem ainda não quer testar deixa nome e WhatsApp para receber o guia grátis."""
import re
from datetime import timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from core import db, now, uid, client_ip, digest, admin

router = APIRouter()
GUIDE = '/guia/7-erros-whatsapp/'

class LeadIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=8, max_length=24)
    segment: str = Field(default='', max_length=40)
    consent: bool = True
    source: str = Field(default='', max_length=200)  # utm / de onde veio
    website: str = Field(default='', max_length=200)  # campo escondido: robô preenche, gente não

@router.post('/site/lead')
async def lead(body: LeadIn, request: Request):
    if body.website: return {'ok': True, 'guide': GUIDE}  # robô: finge que deu certo
    d = re.sub(r'\D', '', body.phone)
    if len(d) in (10, 11): d = '55' + d
    if not (12 <= len(d) <= 13): raise HTTPException(400, 'Confira o WhatsApp: DDD e número, como (11) 91234-5678.')
    key = digest('lead:' + client_ip(request))
    lim = await db.login_limits.find_one({'id': key})
    if lim and lim['count'] >= 8 and lim['expires_at'] > now(): raise HTTPException(429, 'Muitos envios daqui. Tente de novo em 1 hora.')
    await db.login_limits.update_one({'id': key}, {'$inc': {'count': 1}, '$set': {'expires_at': now() + timedelta(hours=1)}}, upsert=True)
    await db.site_leads.update_one({'phone': d}, {'$set': {'name': body.name.strip(), 'segment': body.segment.strip(), 'consent': body.consent,
        'source': body.source.strip(), 'last_at': now()}, '$setOnInsert': {'id': uid(), 'phone': d, 'created_at': now()}, '$inc': {'times': 1}}, upsert=True)
    return {'ok': True, 'guide': GUIDE}

@router.get('/admin/site-leads')
async def leads(user=Depends(admin)):
    rows = await db.site_leads.find({}, {'_id': 0}).sort('last_at', -1).to_list(2000)
    def nd(p):
        x = re.sub(r'\D', '', p or '')
        return '55' + x if len(x) in (10, 11) else x
    signed = {nd(c.get('phone')) for c in await db.companies.find({'phone': {'$nin': [None, '']}}, {'_id': 0, 'phone': 1}).to_list(20000)}
    for r in rows: r['signed_up'] = r['phone'] in signed
    return rows

@router.get('/admin/site-leads.csv')
async def leads_csv(user=Depends(admin)):
    rows = await leads(user)
    q = lambda v: '"' + str(v or '').replace('"', '""') + '"'
    lines = ['nome;whatsapp;segmento;aceita_whatsapp;origem;primeiro_contato;ultimo_contato;criou_conta']
    lines += [';'.join(q(x) for x in (r['name'], r['phone'], r.get('segment'), 'sim' if r.get('consent') else 'não', r.get('source'),
              r['created_at'].strftime('%d/%m/%Y %H:%M'), r['last_at'].strftime('%d/%m/%Y %H:%M'), 'sim' if r['signed_up'] else 'não')) for r in rows]
    return Response('﻿' + '\n'.join(lines), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="leads-do-site.csv"'})
