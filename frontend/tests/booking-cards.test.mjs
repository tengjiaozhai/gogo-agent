import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const require = createRequire(import.meta.url);
async function component(name) {
  const source = readFileSync(new URL(`../src/components/${name}.tsx`, import.meta.url), 'utf8');
  let code = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext } }).outputText;
  for (const dependency of ['react/jsx-runtime', 'react']) {
    const url = JSON.stringify(pathToFileURL(require.resolve(dependency)).href);
    code = code.replaceAll(`"${dependency}"`, url).replaceAll(`'${dependency}'`, url);
  }
  return (await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)).default;
}
const Result = await component('BookingResultCard');
const Confirmation = await component('BookingConfirmationCard');
const pending = { confirmation_id: 'c1', token: 'private-confirmation-token', plan_id: 'p1', content_version: 1,
  proposal_tag: '综合最佳', leg: 'hotel', biz_type: 'HOTEL', title: '酒店示例', route: '上海', start_time: '2026-11-10',
  end_time: '2026-11-12', amount: '640.00', currency: 'CNY', expires_at: Math.floor(Date.now() / 1000) + 600, simulated: true };
const result = { type: 'booking_result', confirmationId: 'c1', bookingId: 'bk1', bizType: 'hotel', bizTypeLabel: '酒店',
  orderId: 'SUP000001', title: '酒店示例', amount: '640.00', currency: 'CNY', route: '上海', startTime: '2026-11-10',
  endTime: '2026-11-12', simulated: true, detail: '来自服务端的预订事实' };

for (const [resolution, label] of [['CONFIRMED', '模拟预订已确认'], ['FAILED', '预订未完成'], ['UNKNOWN', '预订结果待核实']]) {
  test(`预订结果 ${resolution} 按真实状态渲染且没有支付入口`, () => {
    const html = renderToStaticMarkup(React.createElement(Result, { data: { ...result, resolution, statusLabel: label } }));
    assert.ok(html.includes(label));
    assert.ok(html.includes('2026-11-10') && html.includes('640.00'));
    assert.ok(!html.includes('立即支付') && !html.includes('<a '));
    if (resolution !== 'CONFIRMED') assert.ok(!html.includes('模拟预订已确认'));
    if (resolution === 'UNKNOWN') assert.ok(html.includes('不要换方案重复下单'));
  });
}

test('确认卡展示日期金额但不把签名凭据渲染到DOM', () => {
  const html = renderToStaticMarkup(React.createElement(Confirmation, { data: pending, disabled: false, processed: false, onConfirm() {} }));
  assert.ok(html.includes('确认模拟预订') && html.includes('2026-11-12') && html.includes('640.00'));
  assert.ok(!html.includes(pending.token));
});
test('未保存完成的摘要不能操作，已处理和过期摘要没有确认按钮', () => {
  const render = (props) => renderToStaticMarkup(React.createElement(Confirmation, { data: pending, disabled: false, processed: false, onConfirm() {}, ...props }));
  assert.ok(render({ disabled: true }).includes('disabled=""'));
  assert.ok(!render({ processed: true }).includes('<button'));
  const expired = render({ data: { ...pending, expires_at: Math.floor(Date.now() / 1000) - 1 } });
  assert.ok(expired.includes('确认已过期') && !expired.includes('<button'));
});

test('旧格式或不完整预订数据明确报错，不使聊天页面崩溃或开放确认', () => {
  const invalidResult = renderToStaticMarkup(React.createElement(Result, { data: { ...result, resolution: undefined } }));
  assert.ok(invalidResult.includes('历史预订结果格式不完整') && !invalidResult.includes('<a '));
  const invalidConfirmation = renderToStaticMarkup(React.createElement(Confirmation, {
    data: { ...pending, start_time: undefined }, disabled: false, processed: false, onConfirm() {},
  }));
  assert.ok(invalidConfirmation.includes('确认摘要信息不完整') && !invalidConfirmation.includes('<button'));
  const noAmount = renderToStaticMarkup(React.createElement(Confirmation, { data: { ...pending, amount: undefined }, disabled: false, processed: false, onConfirm() {} }));
  assert.ok(noAmount.includes('确认摘要信息不完整') && !noAmount.includes('<button')); 
});

test('未知结果提供核实原单入口，确认和失败状态不提供重购按钮', () => {
  const render = (resolution) => renderToStaticMarkup(React.createElement(Result, {
    data: { ...result, resolution }, onCheck() {}, busy: false,
  }));
  assert.ok(render('UNKNOWN').includes('核实原预订'));
  assert.ok(!render('CONFIRMED').includes('<button') && !render('FAILED').includes('<button'));
});
