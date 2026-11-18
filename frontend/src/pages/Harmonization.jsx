import { useEffect, useState } from 'react'
import { api } from '../api'
import { useAuth } from '../auth'

const ROUTE_LABEL = { traceable: '可溯源（共同标准物质）', bridge: '桥接（配对样本）', none: '不可比' }
const TRACE_LABEL = { traceable: '可溯源', bridged: '桥接' }
const CLASS_LABEL = {
  comparable: '可比', censored_low: '低于检出限（删失）',
  below_range: '低于校准范围', above_range: '高于校准范围',
  qc_failed: '质控失败批次', outside_validity: '有效期外',
  uncalibrated: '无有效校准',
}
const STATUS_TAG = {
  validated: 'ok', active: 'ok', rejected: 'bad', superseded: '',
}

export default function Harmonization() {
  const { user } = useAuth()
  const isAdmin = user?.role === 'data_admin'
  const [platforms, setPlatforms] = useState([])
  const [assays, setAssays] = useState([])
  const [coverage, setCoverage] = useState(null)
  const [detail, setDetail] = useState(null)
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  async function reload() {
    const [p, a, cov] = await Promise.all([
      api.get('/api/harmonization/platforms'),
      api.get('/api/harmonization/assays'),
      api.get('/api/harmonization/coverage'),
    ])
    setPlatforms(p)
    setAssays(a)
    setCoverage(cov)
  }
  useEffect(() => {
    reload().catch((e) => setMsg(e.message))
  }, [])

  async function syncCenter(code) {
    setMsg(''); setBusy(true)
    try {
      await api.post(`/api/harmonization/centers/${code}/sync`)
      setMsg(`${code} 检测平台、质控批次与校准样本已从中心同步`)
      await reload()
    } catch (e) {
      setMsg(`同步失败：${e.message}`)
    } finally { setBusy(false) }
  }

  async function refit(code) {
    setMsg(''); setBusy(true)
    try {
      const m = await api.post(`/api/harmonization/assays/${code}/fit`, {})
      setMsg(`${code} 校准重新拟合为 v${m.version_no}（${m.status}）；新校准仅用于新运行`)
      await reload()
      if (detail) await openDetail(code)
    } catch (e) {
      setMsg(`拟合失败：${e.message}`)
    } finally { setBusy(false) }
  }

  async function openDetail(code) {
    const d = await api.get(`/api/harmonization/assays/${code}`)
    const samples = await api.get(`/api/harmonization/samples/${code}`)
    const models = await api.get(`/api/harmonization/models?center_code=${code}`)
    setDetail({ assay: d, samples, models: models.reverse() })
  }

  return (
    <>
      <div className="panel">
        <h2>跨中心检测口径校准</h2>
        {msg && <div className="alert">{msg}</div>}
        <p className="small muted">
          各中心报告单位同为 mg/L，但检测平台、试剂批次、检出限与校准范围不同。
          只有经留出样本验证的可比值才纳入跨中心统计；超出校准范围、低于检出限、
          质控失败批次或桥接样本不足的值保留明确状态并排除，绝不插补为零。
        </p>
        <h3>校准覆盖（{coverage ? `${coverage.comparable_centers.length}/${coverage.centers.length}` : '—'} 中心可比）</h3>
        <table className="grid">
          <thead>
            <tr><th>中心</th><th>平台</th><th>溯源</th><th>路线</th><th>模型</th><th>状态</th><th>可比范围</th><th>不可比原因</th></tr>
          </thead>
          <tbody>
            {(coverage?.centers || []).map((c) => (
              <tr key={c.center_code}>
                <td className="mono">{c.center_code}</td>
                <td className="mono">{c.platform || '—'}</td>
                <td>{TRACE_LABEL[c.traceability] || '—'}</td>
                <td>{ROUTE_LABEL[c.route] || '—'}</td>
                <td>{c.version_no ? `v${c.version_no}` : '未拟合'}</td>
                <td>
                  <span className={`tag ${c.comparable ? 'ok' : 'bad'}`}>
                    {c.comparable ? '可比' : '不可比'}
                  </span>
                </td>
                <td className="small">
                  {c.range_low != null ? `${c.range_low} – ${c.range_high} mg/L` : '—'}
                </td>
                <td className="small">
                  {(c.reasons || []).length ? c.reasons.join('；') : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>检测平台目录</h2>
        <table className="grid">
          <thead><tr><th>代码</th><th>名称</th><th>方法</th><th>厂商</th><th>溯源</th><th>标准物质</th></tr></thead>
          <tbody>
            {platforms.map((p) => (
              <tr key={p.code}>
                <td className="mono">{p.code}</td>
                <td>{p.name}</td>
                <td>{p.method}</td>
                <td>{p.manufacturer || '—'}</td>
                <td>{TRACE_LABEL[p.traceability] || p.traceability}</td>
                <td className="small">{p.reference_material || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>中心检测登记</h2>
        <table className="grid">
          <thead>
            <tr><th>中心</th><th>平台</th><th>试剂批次</th><th>检出限</th><th>参考区间</th>
              <th>有效期</th><th>质控</th><th>校准样本</th><th>最近模型</th><th>操作</th></tr>
          </thead>
          <tbody>
            {assays.map((a) => (
              <tr key={a.id}>
                <td className="mono">{a.center_code}</td>
                <td className="mono">{a.platform.code}</td>
                <td className="mono">{a.reagent_lot}</td>
                <td>{a.detection_limit}</td>
                <td className="small">
                  {a.reference_interval?.low}–{a.reference_interval?.high}
                </td>
                <td className="small">{a.valid_from} ~ {a.valid_to || '长期'}</td>
                <td className="small">
                  {a.qc_batches.map((q) => (
                    <div key={q.lot}>
                      <span className={`tag ${q.status === 'passed' ? 'ok' : 'bad'}`}>{q.status}</span>{' '}
                      <span className="mono">{q.lot}</span> CV {q.observed_cv_pct ?? '—'}%
                    </div>
                  ))}
                </td>
                <td className="small">
                  参考 {a.sample_counts.reference} / 桥接 {a.sample_counts.bridge}（留出 {a.sample_counts.holdout}）
                </td>
                <td>
                  {a.latest_model ? (
                    <span className={`tag ${STATUS_TAG[a.latest_model.status] || ''}`}>
                      v{a.latest_model.version_no} {a.latest_model.status}
                    </span>
                  ) : '—'}
                </td>
                <td>
                  <div className="row">
                    <button className="link" onClick={() => openDetail(a.center_code)}>详情</button>
                    {isAdmin && (
                      <>
                        <button className="link" disabled={busy}
                          onClick={() => syncCenter(a.center_code)}>同步</button>
                        <button className="link" disabled={busy}
                          onClick={() => refit(a.center_code)}>重新拟合</button>
                      </>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {detail && (
        <div className="modal-mask" onClick={() => setDetail(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>{detail.assay.center_code} · 检测口径与偏差诊断</h3>
            <p className="small muted">
              平台 {detail.assay.platform.code}（{detail.assay.platform.method}）·
              试剂批次 <span className="mono">{detail.assay.reagent_lot}</span> ·
              检出限 {detail.assay.detection_limit} mg/L ·
              校准曲线 a={detail.assay.calibration_curve?.a} b={detail.assay.calibration_curve?.b}
            </p>

            <h4>校准模型版本</h4>
            <table className="grid">
              <thead>
                <tr><th>版本</th><th>路线</th><th>状态</th><th>转换函数</th><th>可比范围</th>
                  <th>留出 RMSE / 最大相对误差</th><th>原始偏差→标准化偏差</th><th>说明</th></tr>
              </thead>
              <tbody>
                {detail.models.map((m) => (
                  <tr key={m.id}>
                    <td className="mono">v{m.version_no}</td>
                    <td>{ROUTE_LABEL[m.route] || m.route}</td>
                    <td><span className={`tag ${STATUS_TAG[m.status] || ''}`}>{m.status}</span></td>
                    <td className="mono small">
                      {m.transform?.enabled
                        ? `y = ${m.transform.intercept} + ${m.transform.slope}·x`
                        : '不可比'}
                    </td>
                    <td className="small">
                      {m.transform?.range_low != null
                        ? `${m.transform.range_low} – ${m.transform.range_high}`
                        : '—'}
                    </td>
                    <td className="small">
                      {m.diagnostics?.rmse ?? '—'} / {m.diagnostics?.max_rel_error ?? '—'}
                    </td>
                    <td className="small">
                      {m.diagnostics?.raw_bias ?? '—'} → {m.diagnostics?.standardized_bias ?? '—'}
                    </td>
                    <td className="small">{m.notes || (m.diagnostics?.reasons?.join('；') || '—')}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            {detail.models[0]?.diagnostics?.by_lot?.length > 0 && (
              <>
                <h4>试剂批次漂移诊断（最近版本）</h4>
                <table className="grid">
                  <thead>
                    <tr><th>批次</th><th>样本数</th><th>质控</th><th>原始均值</th>
                      <th>真实值均值</th><th>原始偏差</th><th>标准化后偏差</th><th>漂移</th></tr>
                  </thead>
                  <tbody>
                    {detail.models[0].diagnostics.by_lot.map((r) => (
                      <tr key={r.lot}>
                        <td className="mono">{r.lot}</td>
                        <td>{r.n}</td>
                        <td><span className={`tag ${r.qc_status === 'passed' ? 'ok' : 'bad'}`}>{r.qc_status}</span></td>
                        <td>{r.raw_mean}</td>
                        <td>{r.truth_mean}</td>
                        <td>{r.raw_bias}</td>
                        <td>{r.standardized_bias}</td>
                        <td>{r.drifted ? <span className="tag warn">漂移</span> : '否'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}

            <h4>去标识化校准/桥接样本</h4>
            <table className="grid">
              <thead>
                <tr><th>样本码</th><th>类型</th><th>参考值</th><th>本平台测量</th>
                  <th>伙伴平台值</th><th>批次</th><th>用途</th></tr>
              </thead>
              <tbody>
                {detail.samples.map((s) => (
                  <tr key={s.sample_code}>
                    <td className="mono">{s.sample_code}</td>
                    <td>{s.kind === 'reference' ? '标准物质' : '桥接'}</td>
                    <td>{s.reference_value ?? '—'}</td>
                    <td>{s.measured_value}</td>
                    <td>{s.partner_value ?? '—'}</td>
                    <td className="mono small">{s.lot}</td>
                    <td>{s.role === 'holdout' ? '留出验证' : '拟合'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="small muted">
              除「{CLASS_LABEL.comparable}」外的状态（{Object.values(CLASS_LABEL).slice(1).join('、')}）
              均不进入跨中心均值，也不会被插补为 0。
            </p>
            <button className="secondary" onClick={() => setDetail(null)}>关闭</button>
          </div>
        </div>
      )}
    </>
  )
}
