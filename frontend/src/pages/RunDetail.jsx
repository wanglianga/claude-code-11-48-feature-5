import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../api'
import { useAuth } from '../auth'
import ResultTable from '../components/ResultTable'

const CENTER_STATUS = {
  success: ['成功', 'ok'], failed: ['失败', 'bad'],
  missing_mapping: ['缺变量映射', 'bad'], pending: ['等待', ''],
}

export default function RunDetail() {
  const { id } = useParams()
  const { user } = useAuth()
  const [run, setRun] = useState(null)
  const [outputs, setOutputs] = useState([])
  const [title, setTitle] = useState('')
  const [msg, setMsg] = useState('')

  async function reload() {
    const r = await api.get(`/api/runs/${id}`)
    setRun(r)
    const os = await api.get('/api/outputs')
    setOutputs(os.filter((o) => o.run_id === r.id))
  }
  useEffect(() => {
    reload().catch((e) => setMsg(e.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id])

  async function createOutput() {
    setMsg('')
    try {
      const o = await api.post(`/api/runs/${id}/outputs`, { title: title || `运行 #${id} 输出表` })
      setMsg(`输出表 #${o.id} 已提交审查（${o.flag_count} 处小样本标记）`)
      await reload()
    } catch (e) {
      setMsg(`创建输出失败：${e.message}`)
    }
  }

  if (!run) return <div className="panel">加载中…</div>
  const cov = run.result.coverage
  const harm = run.result.harmonization || {}
  const harmVars = Object.keys(harm)
  const CAL_STATUS = {
    applied: ['已应用', 'ok'], rejected: ['校准不可用', 'bad'],
    unavailable: ['无校准模型', 'warn'],
  }

  return (
    <>
      <div className="panel">
        <h2>运行 #{run.id}</h2>
        {msg && <div className="alert">{msg}</div>}
        <p className="small muted">
          规则 v{run.rule_version_no} · 条件哈希 <span className="mono">{run.conditions_hash?.slice(0, 16)}…</span>{' '}
          · 请求哈希 <span className="mono">{run.request_hash?.slice(0, 16)}…</span> · k={run.k_threshold}
        </p>
        <h3>队列条件（启动时锁定）</h3>
        <pre className="json">{JSON.stringify(run.conditions, null, 2)}</pre>
        <h3>批次快照（运行启动时锁定，后续批次替换不影响本运行）</h3>
        <table className="grid">
          <thead><tr><th>中心</th><th>批次</th><th>执行状态</th><th>例数</th><th>耗时</th><th>说明</th></tr></thead>
          <tbody>
            {run.center_results.map((c) => {
              const [label, cls] = CENTER_STATUS[c.status] || [c.status, '']
              return (
                <tr key={c.center_code}>
                  <td>{c.center_code} · {c.center_name}</td>
                  <td className="mono">#{c.batch_id}（{c.batch_label}）</td>
                  <td><span className={`tag ${cls}`}>{label}</span></td>
                  <td>{c.total_n ?? '—（未纳入，非零例）'}</td>
                  <td className="small">{c.duration_ms ?? '—'} ms</td>
                  <td className="small">{c.error || ''}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3>覆盖范围</h3>
        {cov.complete ? (
          <div className="alert ok">全部 6 家中心均成功纳入，无覆盖缺口。</div>
        ) : (
          <div className="alert bad">
            以下中心未纳入总计（缺口而非零例）：
            <ul>
              {cov.excluded_centers.map((e) => (
                <li key={e.center}><b>{e.center}</b>（{e.status}）：{e.reason}</li>
              ))}
            </ul>
          </div>
        )}
        <h3>聚合结果（仅汇总通过执行的中心统计量）</h3>
        <ResultTable table={run.result} />

        {run.calibrations?.length > 0 && (
          <>
            <h3>本次运行使用的检测口径版本（启动时快照，后续重新校准不影响本运行）</h3>
            <table className="grid">
              <thead>
                <tr><th>中心</th><th>检测指标</th><th>校准版本</th><th>路线</th>
                  <th>状态</th><th>可比范围</th><th>转换函数</th><th>哈希</th></tr>
              </thead>
              <tbody>
                {run.calibrations.map((c) => {
                  const [label, cls] = CAL_STATUS[c.status] || [c.status, '']
                  return (
                    <tr key={`${c.center_code}-${c.variable}`}>
                      <td className="mono">{c.center_code}</td>
                      <td className="mono small">{c.variable}</td>
                      <td>{c.model_version_no ? `v${c.model_version_no}` : '—'}</td>
                      <td className="small">{c.route || '—'}</td>
                      <td><span className={`tag ${cls}`}>{label}</span></td>
                      <td className="small">
                        {c.range_low != null ? `${c.range_low} – ${c.range_high}` : '—'}
                      </td>
                      <td className="mono small">
                        {c.enabled ? `y = ${c.intercept} + ${c.slope}·x` : '—'}
                      </td>
                      <td className="mono small">{c.transform_hash?.slice(0, 10) || '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </>
        )}

        {harmVars.length > 0 && harmVars.map((v) => {
          const hv = harm[v]
          return (
            <div key={v}>
              <h3>标化覆盖与无法比较比例：<span className="mono">{v}</span></h3>
              <div className="alert">
                纳入可比值 <b>{hv.totals.comparable}</b> / 全部检测值 <b>{hv.totals.values_total}</b>
                （无法比较 {hv.totals.non_comparable}，占{' '}
                {hv.totals.non_comparable_ratio != null
                  ? `${(hv.totals.non_comparable_ratio * 100).toFixed(1)}%` : '—'}）。
                以下分类保留区间/删失状态，不插补为零：
                <div className="small" style={{ marginTop: 4 }}>
                  {Object.entries(hv.totals.counts).map(([k, n]) => (
                    <span key={k} style={{ marginRight: 10 }}>
                      {k} = <b>{n}</b>
                    </span>
                  ))}
                </div>
                {hv.non_comparable_centers.length > 0 && (
                  <div className="small" style={{ marginTop: 4 }}>
                    无可用校准、整体不纳入比较的中心：<b>{hv.non_comparable_centers.join(', ')}</b>
                  </div>
                )}
              </div>
              <table className="grid">
                <thead>
                  <tr><th>中心</th><th>校准版本</th><th>可比</th><th>低于检出限</th>
                    <th>低于范围</th><th>高于范围</th><th>质控失败</th><th>有效期外</th>
                    <th>无校准</th><th>无法比较比例</th></tr>
                </thead>
                <tbody>
                  {Object.entries(hv.centers).map(([code, cv]) => (
                    <tr key={code}>
                      <td className="mono">{code}</td>
                      <td>{cv.transform_version ? `v${cv.transform_version}` : '—'}</td>
                      <td>{cv.counts.comparable}</td>
                      <td>{cv.counts.censored_low}</td>
                      <td>{cv.counts.below_range}</td>
                      <td>{cv.counts.above_range}</td>
                      <td>{cv.counts.qc_failed}</td>
                      <td>{cv.counts.outside_validity}</td>
                      <td>{cv.counts.uncalibrated}</td>
                      <td>
                        {cv.non_comparable_ratio != null
                          ? `${(cv.non_comparable_ratio * 100).toFixed(1)}%` : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        })}
        {user.role === 'researcher' && run.status.startsWith('completed') && (
          <div className="row" style={{ marginTop: 12 }}>
            <input
              className="grow"
              placeholder="输出表标题"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
            <button onClick={createOutput}>提交输出表审查</button>
          </div>
        )}
      </div>

      {outputs.length > 0 && (
        <div className="panel">
          <h3>本运行的输出表</h3>
          <table className="grid">
            <thead><tr><th>ID</th><th>标题</th><th>状态</th><th>小样本标记</th><th>创建时间</th></tr></thead>
            <tbody>
              {outputs.map((o) => (
                <tr key={o.id}>
                  <td className="mono">#{o.id}</td>
                  <td>{o.title}</td>
                  <td><span className={`tag ${o.status === 'approved' ? 'ok' : o.status === 'rejected' ? 'bad' : 'warn'}`}>{o.status}</span></td>
                  <td>{o.flag_count}</td>
                  <td className="small">{o.created_at?.slice(0, 19).replace('T', ' ')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
