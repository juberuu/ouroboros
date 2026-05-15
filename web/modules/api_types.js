/**
 * JSDoc mirror of `ouroboros.gateway.contracts`.
 *
 * This file is intentionally dependency-free: it gives frontend contributors a
 * visible contract surface without adding TypeScript, codegen, or a build step.
 */

/**
 * @typedef {Object} StateResponse
 * @property {number} uptime
 * @property {number} workers_alive
 * @property {number} workers_total
 * @property {number} pending_count
 * @property {number} running_count
 * @property {number} spent_usd
 * @property {number} budget_limit
 * @property {number} budget_pct
 * @property {string} branch
 * @property {string} sha
 * @property {boolean} evolution_enabled
 * @property {boolean} bg_consciousness_enabled
 * @property {number} evolution_cycle
 * @property {Object} evolution_state
 * @property {Object} bg_consciousness_state
 * @property {number} spent_calls
 * @property {boolean} supervisor_ready
 * @property {?string} supervisor_error
 * @property {string} runtime_mode
 * @property {boolean} skills_repo_configured
 * @property {boolean} github_token_configured
 */

/**
 * @typedef {Object} HealthResponse
 * @property {"ok"} status
 * @property {string} version
 * @property {string} runtime_version
 * @property {string} app_version
 */

/**
 * @typedef {Object} ChatInbound
 * @property {"chat"} type
 * @property {string} content
 * @property {string=} sender_session_id
 * @property {string=} client_message_id
 */

/**
 * @typedef {Object} CommandInbound
 * @property {"command"} type
 * @property {string} cmd
 */

/**
 * @typedef {Object} ChatOutbound
 * @property {"chat"} type
 * @property {"user"|"assistant"|"system"} role
 * @property {string} content
 * @property {string} ts
 * @property {boolean=} markdown
 * @property {boolean=} is_progress
 * @property {string=} task_id
 * @property {string=} source
 * @property {string=} sender_label
 * @property {string=} sender_session_id
 * @property {string=} client_message_id
 * @property {Object=} transport
 */

/**
 * @typedef {Object} LogOutbound
 * @property {"log"} type
 * @property {Object} data
 */

/**
 * @typedef {Object} UploadResponse
 * @property {boolean} ok
 * @property {string} filename
 * @property {string} display_name
 * @property {string} path
 * @property {number} size
 * @property {string} mime
 */

export const GATEWAY_CONTRACT_VERSION = '5.22.0-rc.1';
