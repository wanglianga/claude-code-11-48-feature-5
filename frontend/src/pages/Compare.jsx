import { useEffect, useState } from 'react'
import { api } from '../api'
import ResultTable, { visibleMetrics } from '../components/ResultTable'

export default function Compare() {
  const [runs, setRuns] = useState([])
  const [a, setA] = useState('')
  const [b, setB] = useState('')
  const [data, setData] = useState(null)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    api.get('/api/runs').then(setRuns).catch((e) => setMsg(e.message))
  }, [])

  async function compare() {
    setMsg('')
    try {
      setData(await api.get(`/api/runs/compare?a=${a}&b=${b}`))
    } catch (e) {
      setMsg(e.message)
    }
  }

  function batchLine(run) {
    return run.batches.map((x) => `${x.center_code}:#${x.batch_id}(${x.batch_label})`).join('  ')
  }

  function calibrationLine(run) {
    return (run.calibrations || [])
      .map((c) => `${c.center_code}:${c.model_version_no ? `v${c.model_version_no}` : '无'}`)
      .join('  ')
  }

  function deltaRows() {
    if (!data) return []
    const ga = Object.fromEntries(data.a.result.groups.map((g) => [g.label, g]))
    const gb = Object.fromEntries(data.b.result.groups.map((g) => [g.label, g]))
    const labels = [...new Set([...Object.keys(ga), ...Object.keys(gb)])].sort()
    const metrics = [...new Set([
      ...visibleMetrics(data.a.result), ...visibleMetrics(data.b.result),
    ])]
    return { labels, metrics, ga, gb }
  }

  const d = deltaRows()

  return (
    <>
      <div className="panel">
        <h2>运行对比</h2>
        {msg && <div className="alert bad">{msg}</div>}
        <div className="row">
          <select value={a} onChange={(e) => setA(e.target.value)}>
            <option value="">运行 A</option>
            {runs.map((r) => <option key={r.id} value={r.id}>#{r.id} · 规则v{r.rule_version_no} · {r.status}</option>)}
          </select>
          <select value={b} onChange={(e) => setB(e.target.value)}>
            <option value="">运行 B</option>
            {runs.map((r) => <option key={r.id} value={r.id}>#{r.id} · 规则v{r.rule_version_no} · {r.status}</option>)}
          </select>
          <button disabled={!a || !b || a === b} onClick={compare}>对比</button>
        </div>
      </div>

      {data && (
        <>
          <div className="panel">
            <h3>版本与批次</h3>
            <table className="grid">
              <thead><tr><th></th><th>规则版本</th><th>条件哈希</th><th>批次集</th><th>校准版本</th><th>覆盖</th></tr></thead>
              <tbody>
                {['a', 'b'].map((k) => (
                  <tr key={k}>
                    <td>运行 {k.toUpperCase()}（#{data[k].id}）</td>
                    <td>v{data[k].rule_version_no}</td>
                    <td className="mono small">{data[k].conditions_hash?.slice(0, 12)}…</td>
                    <td className="mono small">{batchLine(data[k])}</td>
                    <td className="mono small">{calibrationLine(data[k]) || '—'}</td>
                    <td>{data[k].result.coverage.complete
                      ? '完整'
                      : `缺口：${data[k].result.coverage.excluded_centers.map((e) => e.center).join(', ')}`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="panel">
            <h3>分组指标差异</h3>
            <table className="grid">
              <thead>
                <tr>
                  <th>分组</th>
                  {d.metrics.map((m) => <th key={m}>{m}（A → B）</th>)}
                </tr>
              </thead>
              <tbody>
                {d.labels.map((lbl) => (
                  <tr key={lbl}>
                    <td>{lbl}</td>
                    {d.metrics.map((m) => {
                      const va = m === 'n' ? d.ga[lbl]?.n : d.ga[lbl]?.metrics?.[m]
                      const vb = m === 'n' ? d.gb[lbl]?.n : d.gb[lbl]?.metrics?.[m]
                      const changed = va !== vb
                      return (
                        <td key={m} className={changed ? 'cell-flag' : ''}>
                          {va ?? '—'} → {vb ?? '—'}
                        </td>
                      )
                    })}
                  </tr>
                ))}
                <tr style={{ fontWeight: 600 }}>
                  <td>合计</td>
                  {d.metrics.map((m) => {
                    const va = m === 'n' ? data.a.result.totals.n : data.a.result.totals.metrics?.[m]
                    const vb = m === 'n' ? data.b.result.totals.n : data.b.result.totals.metrics?.[m]
                    return <td key={m} className={va !== vb ? 'cell-flag' : ''}>{va ?? '—'} → {vb ?? '—'}</td>
                  })}
                </tr>
              </tbody>
            </table>
          </div>
          <div className="panel">
            <h3>运行 A 结果表</h3>
            <ResultTable table={data.a.result} />
            <h3>运行 B 结果表</h3>
            <ResultTable table={data.b.result} />
          </div>
        </>
      )}
    </>
  )
}
