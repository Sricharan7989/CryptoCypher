// The money-flow graph. Cytoscape renders the wallets and the transfers
// between them; colour carries the meaning an investigator needs at a glance:
// where the trace started, what is an exchange, and what is a mixer or bridge.

import { useEffect, useRef } from 'react'
import cytoscape from 'cytoscape'
import { findPath, pathAddresses } from '../trace-path.js'

const STYLE = [
  {
    selector: 'node',
    style: {
      'background-color': '#94a3b8',
      label: 'data(display)',
      color: '#475569',
      'font-size': '9px',
      'text-valign': 'bottom',
      'text-margin-y': 4,
      width: 16,
      height: 16,
    },
  },
  {
    selector: 'node[kind = "start"]',
    style: { 'background-color': '#dc2626', width: 30, height: 30, color: '#b91c1c', 'font-size': '11px', 'font-weight': 'bold' },
  },
  {
    selector: 'node[kind = "vasp"]',
    style: { 'background-color': '#16a34a', width: 30, height: 30, color: '#15803d', 'font-size': '11px', 'font-weight': 'bold' },
  },
  {
    selector: 'node[kind = "suspect_vasp"]',
    style: { 'background-color': '#f0fdf4', 'border-color': '#16a34a', 'border-width': 3, width: 22, height: 22 },
  },
  {
    selector: 'node[kind = "mixer"]',
    style: { 'background-color': '#ea580c', width: 26, height: 26, color: '#c2410c', 'font-size': '10px', 'font-weight': 'bold' },
  },
  {
    selector: 'node[kind = "bridge"]',
    style: { 'background-color': '#7c3aed', width: 26, height: 26, color: '#6d28d9', 'font-size': '10px', 'font-weight': 'bold' },
  },
  {
    selector: 'edge',
    style: {
      width: 1,
      'line-color': '#cbd5e1',
      'target-arrow-color': '#cbd5e1',
      'target-arrow-shape': 'triangle',
      'arrow-scale': 0.7,
      'curve-style': 'bezier',
    },
  },
  {
    // The route the Finding panel is describing, lit up so the two views agree.
    selector: 'edge[onPath = 1]',
    style: { width: 3, 'line-color': '#dc2626', 'target-arrow-color': '#dc2626', 'z-index': 10 },
  },
  { selector: 'node[onPath = 1]', style: { 'border-color': '#dc2626', 'border-width': 2 } },
]

function nodeKind(node) {
  if (node.is_start) return 'start'
  if (node.is_mixer) return 'mixer'
  if (node.is_bridge) return 'bridge'
  if (node.entity_type === 'suspected_exchange') return 'suspect_vasp'
  if (node.is_vasp) return 'vasp'
  return 'plain'
}

export default function TraceGraph({ data }) {
  const container = useRef(null)
  const cyRef = useRef(null)

  useEffect(() => {
    if (!container.current || !data) return

    const target = data.summary?.address ?? null
    const path = target ? findPath(data.edges, data.start_address, target) : []
    const onPath = new Set(pathAddresses(path, data.start_address))
    const pathEdges = new Set(path.map((e) => `${e.source}->${e.target}`))

    const elements = [
      ...data.nodes.map((node) => ({
        data: {
          id: node.id,
          // Only labelled entities and the start wallet get text. Labelling all
          // 400 anonymous wallets would be noise, not information.
          display: node.is_start
            ? 'SUSPECT'
            : node.entity_type === 'suspected_exchange'
              ? 'collection point?'
              : (node.label ?? ''),
          kind: nodeKind(node),
          onPath: onPath.has(node.id) ? 1 : 0,
        },
      })),
      ...data.edges.map((edge) => ({
        data: {
          id: `${edge.source}->${edge.target}`,
          source: edge.source,
          target: edge.target,
          onPath: pathEdges.has(`${edge.source}->${edge.target}`) ? 1 : 0,
        },
      })),
    ]

    const cy = cytoscape({
      container: container.current,
      elements,
      style: STYLE,
      layout: {
        name: 'breadthfirst',
        directed: true,
        roots: [data.start_address],
        spacingFactor: 1.1,
        padding: 24,
      },
      minZoom: 0.15,
      maxZoom: 3,
    })

    cy.on('tap', 'node', (evt) => {
      window.open(`https://etherscan.io/address/${evt.target.id()}`, '_blank')
    })

    cyRef.current = cy
    return () => cy.destroy()
  }, [data])

  if (!data) {
    return (
      <div className="graph graph-empty">
        <p className="muted">The money-flow graph will appear here.</p>
      </div>
    )
  }

  return (
    <div className="graph-wrap">
      <div className="graph" ref={container} />
      <div className="legend">
        <span><i className="dot dot-start" /> Suspect wallet</span>
        <span><i className="dot dot-plain" /> Unhosted wallet</span>
        <span><i className="dot dot-vasp" /> Exchange</span>
        <span><i className="dot dot-mixer" /> Mixer</span>
        <span><i className="dot dot-bridge" /> Bridge</span>
        <span className="legend-hint">
          {data.stats.nodes} wallets · {data.stats.edges} transfers · click a wallet to open Etherscan
        </span>
      </div>
    </div>
  )
}
