// The Finding panel - the investigator-facing answer to "where did the money go".
//
// Everything here is written for a police investigator, not a developer. No
// method names, no field names, no confidence floats. The panel has one job:
// state what was found, show the trail that proves it, be honest about how
// certain it is, and offer the next lawful step.

import { useState } from 'react'
import {
  ETHERSCAN,
  describeMethod,
  describeRole,
  findPath,
  formatEth,
  pathAddresses,
  shortAddress,
} from '../trace-path.js'

function ConfidenceBar({ value }) {
  const pct = Math.round((value ?? 0) * 100)
  // Confidence is never presented as certainty. The band names are deliberately
  // cautious, because this output can justify a legal request against a person.
  const band = pct >= 85 ? 'high' : pct >= 60 ? 'moderate' : 'low'
  return (
    <div className="confidence">
      <div className="confidence-head">
        <span className={`confidence-value conf-${band}`}>{pct}%</span>
        <span className="confidence-label">confidence · {band}</span>
      </div>
      <div className="confidence-track">
        <div className={`confidence-fill conf-${band}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

function AddressLink({ address, children }) {
  return (
    <a
      className="addr"
      href={`${ETHERSCAN}${address}`}
      target="_blank"
      rel="noreferrer"
      title={address}
    >
      {children ?? shortAddress(address)}
    </a>
  )
}

function TracedPath({ data, targetAddress }) {
  const path = findPath(data.edges, data.start_address, targetAddress)
  const nodesById = new Map(data.nodes.map((n) => [n.id, n]))

  if (path.length === 0) {
    return (
      <p className="muted">
        No direct route could be reconstructed to this address.
      </p>
    )
  }

  const addresses = pathAddresses(path, data.start_address)

  return (
    <ol className="path">
      {addresses.map((address, index) => {
        const node = nodesById.get(address) ?? {}
        const incoming = index === 0 ? null : path[index - 1]
        const isStart = index === 0
        const isEnd = index === addresses.length - 1

        const role = describeRole(node, isStart)

        const kind = isStart
          ? 'start'
          : node.is_vasp
            ? 'vasp'
            : node.is_mixer || node.is_bridge
              ? 'flag'
              : 'plain'

        return (
          <li key={address} className={`path-step step-${kind}`}>
            <div className="path-marker">
              <span className="path-dot" />
              {!isEnd && <span className="path-line" />}
            </div>
            <div className="path-body">
              <div className="path-role">
                {role}
                {!isStart && (
                  <span className="path-hop">hop {index}</span>
                )}
              </div>
              <AddressLink address={address} />
              {incoming && (
                <div className="path-value">
                  received {formatEth(incoming.value_eth)}
                  {incoming.tx_count > 1 && ` across ${incoming.tx_count} transactions`}
                </div>
              )}
            </div>
          </li>
        )
      })}
    </ol>
  )
}

function Flags({ flags, onPath }) {
  if (!flags || flags.length === 0) return null
  return (
    <section className="block">
      <h3>Obfuscation encountered</h3>
      <ul className="flags">
        {flags.map((flag) => (
          <li key={flag.address} className="flag">
            <span className="flag-icon" aria-hidden="true">!</span>
            <div>
              <div className="flag-name">
                {flag.entity}
                {onPath.has(flag.address) && (
                  <span className="flag-tag">on this path</span>
                )}
              </div>
              <div className="flag-note">
                {flag.entity_type === 'mixer'
                  ? `${formatEth(flag.value_received_eth)} entered a mixing service. Funds sent through a mixer cannot be followed further on-chain.`
                  : `${formatEth(flag.value_received_eth)} moved to another blockchain through a bridge. The trail continues outside Ethereum.`}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}

function MethodNote({ method }) {
  const described = describeMethod(method)
  return (
    <section className="block">
      <h3>How this was identified</h3>
      <div className={`method ${described.strong ? 'method-strong' : 'method-weak'}`}>
        <div className="method-title">{described.title}</div>
        <p className="method-detail">{described.detail}</p>
      </div>
    </section>
  )
}

export default function FindingPanel({ data, loading, error, onToast }) {
  const [routed, setRouted] = useState(false)

  if (loading) {
    return (
      <aside className="panel">
        <div className="panel-empty">
          <div className="spinner" />
          <p>Following the money…</p>
          <p className="muted">
            Reading public transaction records hop by hop. This can take a minute
            on a busy wallet.
          </p>
        </div>
      </aside>
    )
  }

  if (error) {
    return (
      <aside className="panel">
        <div className="panel-empty">
          <h2 className="finding-none">Trace could not run</h2>
          <p className="muted">{error}</p>
        </div>
      </aside>
    )
  }

  if (!data) {
    return (
      <aside className="panel">
        <div className="panel-empty">
          <h2>No trace yet</h2>
          <p className="muted">
            Enter a suspect wallet address to follow its funds forward through
            the blockchain and find the exchange where they landed.
          </p>
        </div>
      </aside>
    )
  }

  const summary = data.summary ?? {}
  const target = summary.address ?? null
  const path = target ? findPath(data.edges, data.start_address, target) : []
  const onPath = new Set(pathAddresses(path, data.start_address))

  const handleRoute = () => {
    setRouted(true)
    onToast(
      `Request prepared for ${summary.exchange ?? 'the identified exchange'} — ` +
        `simulated SAHYOG routing, nothing was actually sent.`,
    )
  }

  // --- Nothing recognised -----------------------------------------------
  if (!summary.found && !summary.lead) {
    return (
      <aside className="panel">
        <header className="finding-head none">
          <div className="eyebrow">Finding</div>
          <h2 className="finding-none">No exchange reached</h2>
          <p className="finding-sub">
            The funds did not arrive at any exchange we recognise within{' '}
            {data.params.max_depth} hops.
          </p>
        </header>
        <div className="panel-body">
          <Flags flags={data.flags} onPath={onPath} />
          <section className="block">
            <h3>What to do next</h3>
            <p className="muted">{summary.recommended_action}</p>
          </section>
        </div>
      </aside>
    )
  }

  // --- Unconfirmed lead --------------------------------------------------
  if (!summary.found && summary.lead) {
    return (
      <aside className="panel">
        <header className="finding-head lead">
          <div className="eyebrow">Finding · unconfirmed</div>
          <h2 className="finding-title">
            A collection point was found {summary.hop_distance} hop
            {summary.hop_distance === 1 ? '' : 's'} away
          </h2>
          <p className="finding-sub">
            This is a lead, not an identified exchange. It has not been named and
            must be verified before any request is raised.
          </p>
          <ConfidenceBar value={summary.confidence} />
        </header>

        <div className="panel-body">
          <section className="block">
            <h3>Traced path</h3>
            <TracedPath data={data} targetAddress={target} />
          </section>

          <MethodNote method={summary.method} />
          <Flags flags={data.flags} onPath={onPath} />

          <section className="block">
            <h3>Recommended action</h3>
            <div className="action action-caution">
              <p>{summary.recommended_action}</p>
            </div>
          </section>
        </div>
      </aside>
    )
  }

  // --- Confirmed exchange -------------------------------------------------
  return (
    <aside className="panel">
      <header className="finding-head found">
        <div className="eyebrow">Finding</div>
        <h2 className="finding-title">
          Funds reached <strong>{summary.exchange}</strong>
          <span className="finding-hops">
            {summary.hop_distance} hop{summary.hop_distance === 1 ? '' : 's'} away
          </span>
        </h2>
        <ConfidenceBar value={summary.confidence} />
        <div className="finding-amount">
          {formatEth(summary.value_received_eth)} traced into this exchange
        </div>
      </header>

      <div className="panel-body">
        <section className="block">
          <h3>Traced path</h3>
          <TracedPath data={data} targetAddress={target} />
        </section>

        <MethodNote method={summary.method} />
        <Flags flags={data.flags} onPath={onPath} />

        {summary.other_exchanges_reached?.length > 0 && (
          <section className="block">
            <h3>Other exchanges reached</h3>
            <p className="muted">
              Funds also arrived at {summary.other_exchanges_reached.join(', ')}.
              Each may hold records worth requesting.
            </p>
          </section>
        )}

        <section className="block">
          <h3>Recommended action</h3>
          <div className="action">
            <p>
              Route a lawful data request to <strong>{summary.exchange}</strong>{' '}
              via SAHYOG for the KYC records behind deposits to{' '}
              <AddressLink address={summary.address} />.
            </p>
            <button
              className="sahyog"
              onClick={handleRoute}
              disabled={routed}
              type="button"
            >
              {routed ? 'Request prepared' : `Route to SAHYOG`}
            </button>
            <p className="action-note">
              Simulated for this demo — no request leaves this machine.
            </p>
          </div>
        </section>
      </div>
    </aside>
  )
}
