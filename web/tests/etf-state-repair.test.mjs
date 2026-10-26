import assert from 'node:assert/strict';
import test from 'node:test';
import { mergeSnapshotIntoEtfFlowState } from '../app/lib/etf-flow-state.ts';

function scene(){
  const instruments=[{code:'510300',market:'SSE',name:'沪深300ETF',category:'宽基',groupType:'broad',assetClass:'股票'},
    {code:'159915',market:'SZSE',name:'创业板ETF',category:'宽基',groupType:'broad',assetClass:'股票'}];
  const state={version:1,classificationVersion:'2026-07-v3',dates:['2026-10-19','2026-10-20','2026-10-23'],
    instruments:instruments.map((row,i)=>({...row,values:[.1,i===0?.2:null,.3],cashValues:[1,i===0?2:null,3]})),
    sourceSnapshots:[],quality:{rejectedRows:0,duplicateRows:0}};
  const snapshot={asOf:'2026-10-20',etfTerminal:{classificationVersion:'2026-07-v3',
    instruments:instruments.map(row=>({...row,periods:{'1d':999},estimatedCashPeriods:{'1d':2}}))}};
  return {state,snapshot};
}

test('verified historical daily values repair existing date and survive later ordinary merge',()=>{
  const {state,snapshot}=scene();
  const changes=[{market:'SSE',code:'510300',value:.2,expectedPreviousTradeDate:'2026-10-19'},
    {market:'SZSE',code:'159915',value:.4,expectedPreviousTradeDate:'2026-10-19'}];
  const repaired=mergeSnapshotIntoEtfFlowState(state,snapshot,'a'.repeat(64),changes);
  assert.deepEqual(repaired.dates,state.dates);
  assert.deepEqual(repaired.instruments.map(r=>r.values),[[.1,.2,.3],[.1,.4,.3]]);
  assert.equal(state.instruments[1].values[1],null); // Pure projection, no input mutation.
  const again=mergeSnapshotIntoEtfFlowState(repaired,snapshot,'a'.repeat(64),changes);
  assert.deepEqual(again.instruments,repaired.instruments);
  assert.deepEqual(again.sourceSnapshots,repaired.sourceSnapshots);
  const next={...snapshot,asOf:'2026-10-26'};
  const advanced=mergeSnapshotIntoEtfFlowState(repaired,next,'b'.repeat(64),changes.map(r=>({...r,value:.5,expectedPreviousTradeDate:'2026-10-23'})));
  assert.deepEqual(advanced.instruments.map(r=>r.values),[[.1,.2,.3,.5],[.1,.4,.3,.5]]);
});

test('explicit source-null revision invalidates target only; missing map entry never means zero',()=>{
  const {state,snapshot}=scene();
  const repaired=mergeSnapshotIntoEtfFlowState(state,snapshot,'c'.repeat(64),[
    {market:'SSE',code:'510300',value:null,expectedPreviousTradeDate:'2026-10-19'}]);
  assert.deepEqual(repaired.instruments[0].values,[.1,null,.3]);
  assert.deepEqual(repaired.instruments[0].cashValues,[1,null,3]);
  assert.deepEqual(repaired.instruments[1].values,state.instruments[1].values);
  assert.equal(repaired.instruments[0].values.includes(999),false);
  assert.equal(state.instruments[0].values[1],.2);
  const withoutRemoved={...snapshot,etfTerminal:{...snapshot.etfTerminal,
    instruments:snapshot.etfTerminal.instruments.filter(row=>row.market!=='SSE')}};
  const removed=mergeSnapshotIntoEtfFlowState(state,withoutRemoved,'d'.repeat(64),[
    {market:'SSE',code:'510300',value:null,expectedPreviousTradeDate:'2026-10-19'}]);
  assert.deepEqual(removed.instruments[0].values,[.1,null,.3]);
});
