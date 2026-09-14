// Helpers for reading a trace response.
//
// The backend returns the whole money-flow graph plus the attributions. The
// panel needs one more thing: the actual chain of wallets from the suspect to
// the exchange, hop by hop. That is derived here rather than round-tripping to
// the server, since the edge list already contains everything required.

/**
 * Shortest path of hops from `start` to `target`, following edge direction.
 *
 * Breadth-first, which matters: the backend reports hop distance from a BFS
 * walk, so the path shown to the investigator has to be the shortest one too.
 * Anything else would contradict the "N hops away" headline.
 *
 * Returns an array of edge objects in order, or [] if no route exists.
 */
export function findPath(edges, start, target) {
  if (!start || !target || start === target) return []

  const outgoing = new Map()
  for (const edge of edges) {
    if (!outgoing.has(edge.source)) outgoing.set(edge.source, [])
    outgoing.get(edge.source).push(edge)
  }

  const queue = [start]
  const cameFrom = new Map() // node -> edge that reached it
  const seen = new Set([start])

  while (queue.length > 0) {
    const current = queue.shift()
    if (current === target) break

    for (const edge of outgoing.get(current) ?? []) {
      if (seen.has(edge.target)) continue
      seen.add(edge.target)
      cameFrom.set(edge.target, edge)
      queue.push(edge.target)
    }
  }

  if (!cameFrom.has(target)) return []

  const path = []
  let node = target
  while (node !== start) {
    const edge = cameFrom.get(node)
    if (!edge) return []
    path.unshift(edge)
    node = edge.source
  }
  return path
}

/** Every address touched by a path, including the starting wallet. */
export function pathAddresses(path, start) {
  const addresses = [start]
  for (const edge of path) addresses.push(edge.target)
  return addresses
}

/**
 * The address the headline finding points at, whichever form the result took.
 * A confirmed exchange and an unconfirmed lead both carry `address`.
 */
export function primaryAddress(summary) {
  return summary?.address ?? null
}

/** 0x1234…cdef - short enough to scan, long enough to compare by eye. */
export function shortAddress(address) {
  if (!address || address.length < 12) return address ?? ''
  return `${address.slice(0, 8)}…${address.slice(-6)}`
}

/** Plain English for how an attribution was made. No jargon in the panel. */
export function describeMethod(method) {
  switch (method) {
    case 'known_label':
      return {
        title: 'Matched against known exchange wallets',
        detail:
          'This address appears on our register of published exchange wallets. ' +
          'Exchanges cannot hide these - they must publish deposit addresses to ' +
          'their customers.',
        strong: true,
      }
    case 'consolidation':
      return {
        title: 'Deposit-consolidation pattern',
        detail:
          'Many separate wallets funnel into this one address, which is how an ' +
          'exchange sweeps customer deposits. The exchange has not been named, ' +
          'and a criminal re-pooling their own funds looks the same.',
        strong: false,
      }
    default:
      return {
        title: 'Not identified',
        detail: 'No identification method recognised this address.',
        strong: false,
      }
  }
}

/**
 * What to call a wallet in the traced path, in the investigator's language.
 *
 * The backend names a consolidation hit "Unknown exchange (consolidation
 * pattern)" - accurate, but it reads like debug output. Here it becomes plain
 * language that still refuses to name a company, because the whole point of
 * that result is that no company has been identified.
 */
export function describeRole(node, isStart) {
  if (isStart) return 'Suspect wallet'
  if (!node) return 'Intermediate wallet'
  if (node.entity_type === 'suspected_exchange') return 'Possible collection point'
  if (node.is_vasp) return node.label ?? 'Exchange'
  if (node.is_mixer) return `${node.label} · mixer`
  if (node.is_bridge) return `${node.label} · bridge`
  return 'Intermediate wallet'
}

export function formatEth(value) {
  if (value === null || value === undefined) return '—'
  if (value >= 1000) return `${value.toLocaleString('en-US', { maximumFractionDigits: 0 })} ETH`
  if (value >= 1) return `${value.toFixed(2)} ETH`
  return `${value.toFixed(4)} ETH`
}

export const ETHERSCAN = 'https://etherscan.io/address/'
