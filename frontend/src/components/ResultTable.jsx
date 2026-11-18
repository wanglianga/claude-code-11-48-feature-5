const METRIC_LABEL = {
  n: '人数',
  outcome_remission_n: '缓解人数',
  outcome_remission_rate: '缓解率',
  'followup.lab_value_mean': '实验室指标均值（原始）',
  'followup.lab_value_std_mean': '实验室指标标准化均值（可比）',
  'followup.lab_value_std_n': '可比例数',
}

export function metricLabel(m) {
  return METRIC_LABEL[m] || m
}

export function visibleMetrics(table) {
  const keys = new Set()
  for (const g of table.groups || []) {
    for (const k of Object.keys(g.metrics || {})) {
      if (!k.startsWith('_')) keys.add(k)
    }
  }
  return [...keys]
}

export default function ResultTable({ table, flags = [] }) {
  if (!table) return null
  const metrics = visibleMetrics(table)
  const flagged = new Set(flags.map((f) => `${f.group}|${f.metric}`))
  const totalsSuppressed = new Set(table.totals?.suppressed || [])

  function cell(group, metric, value) {
    const suppressed = group?.suppressed?.includes(metric)
    const isFlagged = group && flagged.has(`${group.label}|${metric}`)
    const cls = suppressed ? 'cell-suppressed' : isFlagged ? 'cell-flag' : ''
    const text = suppressed || value === null || value === undefined ? '⊘' : value
    return <td className={cls}>{text}</td>
  }

  return (
    <table className="grid">
      <thead>
        <tr>
          <th>分组</th>
          <th>{metricLabel('n')}</th>
          {metrics.map((m) => <th key={m}>{metricLabel(m)}</th>)}
        </tr>
      </thead>
      <tbody>
        {(table.groups || []).map((g) => (
          <tr key={g.label}>
            <td>{g.label}</td>
            {cell(g, 'n', g.n)}
            {metrics.map((m) => (
              <td key={m} className={
                g.suppressed?.includes(m) ? 'cell-suppressed'
                  : flagged.has(`${g.label}|${m}`) ? 'cell-flag' : ''
              }>
                {g.suppressed?.includes(m) || g.metrics[m] === null || g.metrics[m] === undefined
                  ? '⊘' : g.metrics[m]}
              </td>
            ))}
          </tr>
        ))}
        <tr style={{ fontWeight: 600, background: '#f8fafc' }}>
          <td>合计（仅成功中心）</td>
          <td className={totalsSuppressed.has('n') ? 'cell-suppressed' : ''}>
            {totalsSuppressed.has('n') || table.totals?.n === null ? '⊘' : table.totals?.n}
          </td>
          {metrics.map((m) => (
            <td key={m} className={totalsSuppressed.has(m) ? 'cell-suppressed' : ''}>
              {totalsSuppressed.has(m) || table.totals?.metrics?.[m] === null
                || table.totals?.metrics?.[m] === undefined
                ? '⊘' : table.totals.metrics[m]}
            </td>
          ))}
        </tr>
      </tbody>
    </table>
  )
}
