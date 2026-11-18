import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'

const RUN_STATUS = {
  pending: ['排队中', ''], running: ['运行中', 'warn'],
  completed: ['完成', 'ok'], completed_with_gaps: ['完成（有覆盖缺口）', 'warn'],
  failed: ['失败', 'bad'],
}

export default function Runs() {
  const [runs, setRuns] = useState([])
  const [rules, setRules] = useState([])
  const [variables, setVariables] = useState([])
  const [ruleId, setRuleId] = useState('')
  const [versionId, setVersionId] = useState('')
  const [approved, setApproved] = useState([])
  const [groupBy, setGroupBy] = useState(['treatment.path'])
  const [metrics, setMetrics] = useState(['count', 'remission'])
  const [msg, setMsg] = useState('')

  async function reload() {
    const [rs, rl, d] = await Promise.all([
      api.get('/api/runs'), api.get('/api/rules'), api.get('/api/meta/dictionary'),
    ])
    setRuns(rs)
    setRules(rl)
    setVariables(d.variables.filter((v) => v.value_type === 'categorical'))
  }
  useEffect(() => {
    reload().catch((e) => setMsg(e.message))
  }, [])

  useEffect(() => {
    if (!ruleId) {
      setApproved([])
      setVersionId('')
      return
    }
    api.get(`/api/rules/${ruleId}`).then((r) => {
      const ok = r.versions.filter((v) => v.status === 'approved')
      setApproved(ok)
      setVersionId(ok[0]?.id || '')
    })
  }, [ruleId])

  function toggle(list, setList, v) {
    setList(list.includes(v) ? list.filter((x) => x !== v) : [...list, v])
  }

  async function createRun() {
    setMsg('')
    const ms = []
    if (metrics.includes('count')) ms.push({ type: 'count' })
    if (metrics.includes('remission')) {
      ms.push({ type: 'outcome_count', variable: 'followup.outcome', value: 'remission' })
    }
    if (metrics.includes('lab')) ms.push({ type: 'mean', variable: 'followup.lab_value' })
    if (metrics.includes('std')) {
      ms.push({ type: 'standardized_mean', variable: 'followup.lab_value' })
    }
    try {
      const run = await api.post('/api/runs', {
        rule_version_id: Number(versionId),
        request_spec: { group_by: groupBy, metrics: ms },
      })
      setMsg(`运行 #${run.id} 已创建：${run.status}`)
      await reload()
    } catch (e) {
      setMsg(`创建失败：${e.message}`)
    }
  }

  return (
    <>
      <div className="panel">
        <h2>新建运行</h2>
        {msg && <div className="alert">{msg}</div>}
        <div className="row">
          <label className="field grow">
            队列规则（仅已批准版本可运行）
            <select value={ruleId} onChange={(e) => setRuleId(e.target.value)}>
              <option value="">选择规则</option>
              {rules.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
            </select>
          </label>
          <label className="field">
            已批准版本
            <select value={versionId} onChange={(e) => setVersionId(e.target.value)}>
              {approved.length === 0 && <option value="">无已批准版本</option>}
              {approved.map((v) => <option key={v.id} value={v.id}>v{v.version_no}</option>)}
            </select>
          </label>
        </div>
        <div className="row">
          <span>分组：</span>
          {variables.map((v) => (
            <label key={v.name}>
              <input
                type="checkbox"
                checked={groupBy.includes(v.name)}
                onChange={() => toggle(groupBy, setGroupBy, v.name)}
              />{' '}
              {v.label}
            </label>
          ))}
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          <span>统计量：</span>
          <label>
            <input type="checkbox" checked={metrics.includes('count')}
              onChange={() => toggle(metrics, setMetrics, 'count')} /> 人数
          </label>
          <label>
            <input type="checkbox" checked={metrics.includes('remission')}
              onChange={() => toggle(metrics, setMetrics, 'remission')} /> 缓解率
          </label>
          <label>
            <input type="checkbox" checked={metrics.includes('lab')}
              onChange={() => toggle(metrics, setMetrics, 'lab')} /> 实验室指标均值（原始，C5 缺映射）
          </label>
          <label>
            <input type="checkbox" checked={metrics.includes('std')}
              onChange={() => toggle(metrics, setMetrics, 'std')} /> 实验室指标标准化均值（跨中心可比）
          </label>
          <button disabled={!versionId || groupBy.length === 0} onClick={createRun}>
            启动运行
          </button>
        </div>
      </div>

      <div className="panel">
        <h2>运行列表</h2>
        <table className="grid">
          <thead>
            <tr><th>ID</th><th>规则版本</th><th>状态</th><th>中心</th><th>创建时间</th><th></th></tr>
          </thead>
          <tbody>
            {runs.map((r) => {
              const [label, cls] = RUN_STATUS[r.status] || [r.status, '']
              return (
                <tr key={r.id}>
                  <td className="mono">#{r.id}</td>
                  <td>规则#{r.rule_id} · v{r.rule_version_no}</td>
                  <td><span className={`tag ${cls}`}>{label}</span></td>
                  <td>
                    {r.centers_total - r.centers_failed}/{r.centers_total} 成功
                    {r.centers_failed > 0 && <span className="tag bad" style={{ marginLeft: 6 }}>{r.centers_failed} 缺口</span>}
                  </td>
                  <td className="small">{r.created_at?.slice(0, 19).replace('T', ' ')}</td>
                  <td><Link to={`/runs/${r.id}`}>详情</Link></td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </>
  )
}
