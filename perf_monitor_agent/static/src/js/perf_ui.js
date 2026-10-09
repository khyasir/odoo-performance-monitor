/** @odoo-module **/
/**
 * perf_monitor_agent: tells the server which screen an RPC comes from, so that a slow call can be
 * reported as "Dashboard X > Tile Y (menu Z) by user U at HH:MM took N s". Every RPC gets an X-Perf-UI
 * header (a=<action>&m=<menu>&mo=<model>&id=<record>&v=<view type>&ai=<active id>&d=<spreadsheet dashboard>
 * &dh=<odoo_dashboards_saas dashboard>&t=<dashboard tile>); the server only reads it when the call was
 * slow (perf_monitor_agent/tools/ui_context.py).
 *
 * The URL hash is only updated once the new action is mounted, i.e. AFTER its first (often slowest)
 * RPCs, so the action being opened (doAction / doActionButton) and the menu last clicked
 * (selectMenu) are tracked here directly.
 *
 * It must never break the web client: every step is guarded and the original rpc / doAction /
 * selectMenu are always called with their original arguments.
 */
import { browser } from "@web/core/browser/browser";
import { rpcService } from "@web/core/network/rpc_service";
import { registry } from "@web/core/registry";

export const HEADER = "X-Perf-UI";
const MAX_VALUE = 128;
const MAX_HEADER = 512;
const HASH_KEYS = [
    ["action", "a"],
    ["model", "mo"],
    ["id", "id"],
    ["view_type", "v"],
    ["active_id", "ai"],
    ["dashboard_id", "d"],
];

const SAAS_DASHBOARD_MODEL = "dashboards.saas";
const TILE_MODEL = "data.analytics.saas";

/** state shared by the rpc wrapper and the tracker service */
export const perfUiState = { router: null, action: null, pending: null, menuId: null };

function isSet(value) {
    return value !== undefined && value !== null && value !== "" && value !== false;
}

/**
 * odoo_dashboards_saas ids an RPC is about: the tile of a per-tile call (dashboards.saas
 * get_*_from_da_id(null, tileId, ...) or a data.analytics.saas record call).
 * @returns {number|null}
 */
export function tileOfRpc(params) {
    const args = params && Array.isArray(params.args) ? params.args : null;
    if (!args) {
        return null;
    }
    let id = null;
    if (params.model === SAAS_DASHBOARD_MODEL && /_from_da_id$/.test(params.method || "")) {
        id = args[1];
    } else if (params.model === TILE_MODEL && Array.isArray(args[0]) && args[0].length === 1) {
        id = args[0][0];
    }
    return Number.isInteger(id) && id > 0 ? id : null;
}

/** odoo_dashboards_saas dashboard shown by the current client action (its context_dashboard_id) */
export function currentDashboardId(actionService) {
    const controller = actionService && actionService.currentController;
    const context = controller && controller.action && controller.action.context;
    const id = context && typeof context === "object" ? context.context_dashboard_id : null;
    return Number.isInteger(id) && id > 0 ? id : null;
}

/**
 * @param {Object} [hash] current URL hash (router.current.hash)
 * @param {{action: number|string}|null} [pending] action being opened (hash not updated yet)
 * @param {number|null} [menuId] last clicked menu
 * @param {{dashboardId: number|null, tileId: number|null}} [extra] dashboard shown / tile of the RPC
 * @returns {string} header value ("" when there is nothing to send)
 */
export function buildPerfUiHeader(hash, pending, menuId, extra) {
    const out = new URLSearchParams();
    if (pending) {
        out.set("a", String(pending.action).slice(0, MAX_VALUE));
    } else if (hash) {
        for (const [key, short] of HASH_KEYS) {
            if (isSet(hash[key])) {
                out.set(short, String(hash[key]).slice(0, MAX_VALUE));
            }
        }
    }
    const menu = menuId || (hash && hash.menu_id);
    if (isSet(menu)) {
        out.set("m", String(menu).slice(0, MAX_VALUE));
    }
    if (extra && !pending && isSet(extra.dashboardId)) {
        out.set("dh", String(extra.dashboardId));
    }
    if (extra && isSet(extra.tileId)) {
        out.set("t", String(extra.tileId));
    }
    return out.toString().slice(0, MAX_HEADER);
}

function actionKey(request) {
    if (typeof request === "number" || typeof request === "string") {
        return request;
    }
    if (request && typeof request === "object" && request.type !== "ir.actions.act_window_close") {
        return request.id || request.xml_id || request.tag || null;
    }
    return null;
}

function track(key) {
    if (!isSet(key)) {
        return null;
    }
    const mine = { action: key };
    perfUiState.pending = mine;
    return mine;
}

function untrack(mine, result) {
    if (!mine) {
        return;
    }
    // after the router's own (setTimeout-debounced) hash update, scheduled when the action mounted
    const clear = () =>
        browser.setTimeout(() => {
            if (perfUiState.pending === mine) {
                perfUiState.pending = null;
            }
        });
    Promise.resolve(result).then(clear, clear);
}

const perfRpcService = {
    ...rpcService,
    start(env) {
        const rpc = rpcService.start(env);
        return function perfRpc(route, params = {}, settings) {
            let finalSettings = settings;
            try {
                const router = perfUiState.router;
                const value = buildPerfUiHeader(
                    router && router.current && router.current.hash,
                    perfUiState.pending,
                    perfUiState.menuId,
                    { dashboardId: currentDashboardId(perfUiState.action), tileId: tileOfRpc(params) }
                );
                if (value) {
                    const headers = Object.assign({}, settings && settings.headers, { [HEADER]: value });
                    finalSettings = Object.assign({}, settings, { headers });
                }
            } catch (_e) {
                finalSettings = settings;
            }
            return rpc(route, params, finalSettings);
        };
    },
};

const perfUiTracker = {
    dependencies: ["action", "menu", "router"],
    start(env, { action, menu, router }) {
        perfUiState.router = router;
        perfUiState.action = action;
        try {
            const doAction = action.doAction;
            action.doAction = function (actionRequest) {
                let mine = null;
                try {
                    mine = track(actionKey(actionRequest));
                } catch (_e) {
                    mine = null;
                }
                const result = doAction.apply(this, arguments);
                try {
                    untrack(mine, result);
                } catch (_e) {
                    // ignore
                }
                return result;
            };
            const doActionButton = action.doActionButton;
            action.doActionButton = function (params) {
                let mine = null;
                try {
                    mine = params && params.type === "action" ? track(params.name) : null;
                } catch (_e) {
                    mine = null;
                }
                const result = doActionButton.apply(this, arguments);
                try {
                    untrack(mine, result);
                } catch (_e) {
                    // ignore
                }
                return result;
            };
            const selectMenu = menu.selectMenu;
            menu.selectMenu = function (menuOrId) {
                try {
                    const id = typeof menuOrId === "number" ? menuOrId : menuOrId && menuOrId.id;
                    perfUiState.menuId = id || null;
                } catch (_e) {
                    // ignore
                }
                return selectMenu.apply(this, arguments);
            };
        } catch (_e) {
            // tracking unavailable: the header falls back to the URL hash
        }
        return perfUiState;
    },
};

registry.category("services").add("rpc", perfRpcService, { force: true });
registry.category("services").add("perf_monitor_agent_ui", perfUiTracker);
