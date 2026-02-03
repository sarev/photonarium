/**
 * AppState Core - Transaction System and Utilities
 * =================================================
 *
 * Foundation for the AppState architecture. Provides:
 * - Subscriber system for reactive updates
 * - Transaction batching for optimistic updates
 * - localStorage persistence helpers
 *
 * @fileoverview Core infrastructure shared by all AppState domains.
 */

'use strict';

/**
 * Global AppState object - domains are added by subsequent scripts.
 * @namespace AppState
 */
const AppState = (function() {

    /** Enable verbose logging for debugging */
    const DEBUG = false;

    /**
     * Log helper that respects DEBUG flag.
     * @param {string} domain - Domain name for prefix
     * @param {string} method - Method name
     * @param {...*} args - Arguments to log
     */
    function log(domain, method, ...args) {
        if (DEBUG) {
            console.log(`[AppState.${domain}.${method}]`, ...args);
        }
    }

    // =========================================================================
    // SUBSCRIBER SYSTEM
    // =========================================================================

    /**
     * Create a subscriber management system for a domain.
     *
     * Provides pub/sub functionality for reactive state updates.
     * Subscribers are notified when domain state changes.
     *
     * @returns {Object} Subscriber system with subscribe, broadcast, etc.
     * @returns {Function} .subscribe - Add a change listener, returns unsubscribe fn
     * @returns {Function} .subscribeError - Add an error listener
     * @returns {Function} .broadcast - Notify all subscribers
     * @returns {Function} .notify - Alias for broadcast
     * @returns {Function} .broadcastError - Notify error subscribers
     *
     * @example
     * const { subscribe, broadcast } = createSubscriberSystem();
     * const unsub = subscribe(event => console.log('Changed:', event));
     * broadcast({ type: 'changed', property: 'name' });
     * unsub(); // Stop listening
     */
    function createSubscriberSystem() {
        /** @type {Set<Function>} */
        const subscribers = new Set();
        /** @type {Set<Function>} */
        const errorSubscribers = new Set();

        /**
         * Notify all subscribers of a state change.
         * @param {Object} event - Event object
         * @param {string} event.type - Event type (e.g., 'changed', 'loading')
         * @param {string} [event.property] - Specific property that changed
         */
        function notify(event = { type: 'changed' }) {
            for (const callback of subscribers) {
                try {
                    callback(event);
                } catch (err) {
                    console.error('[AppState] Subscriber error:', err);
                }
            }
        }

        return {
            /**
             * Subscribe to state changes.
             * @param {Function} callback - Called with event object on changes
             * @returns {Function} Unsubscribe function
             */
            subscribe(callback) {
                subscribers.add(callback);
                return () => subscribers.delete(callback);
            },

            /**
             * Subscribe to errors.
             * @param {Function} callback - Called with error event
             * @returns {Function} Unsubscribe function
             */
            subscribeError(callback) {
                errorSubscribers.add(callback);
                return () => errorSubscribers.delete(callback);
            },

            broadcast: notify,
            notify,

            /**
             * Broadcast an error to error subscribers.
             * @param {string} message - Error message
             */
            broadcastError(message) {
                const event = { type: 'error', message };
                for (const callback of errorSubscribers) {
                    try {
                        callback(event);
                    } catch (err) {
                        console.error('[AppState] Error subscriber error:', err);
                    }
                }
            }
        };
    }

    // =========================================================================
    // STORAGE HELPERS
    // =========================================================================

    /**
     * localStorage helper with JSON serialization and error handling.
     * All keys are prefixed with 'imaginary-' to avoid collisions.
     */
    const storage = {
        /**
         * Get a value from localStorage.
         * @param {string} key - Storage key (without prefix)
         * @param {*} defaultValue - Value to return if key doesn't exist
         * @returns {*} Parsed value or defaultValue
         */
        get(key, defaultValue) {
            try {
                const value = localStorage.getItem(`imaginary-${key}`);
                return value !== null ? JSON.parse(value) : defaultValue;
            } catch {
                return defaultValue;
            }
        },

        /**
         * Set a value in localStorage.
         * @param {string} key - Storage key (without prefix)
         * @param {*} value - Value to store (will be JSON serialized)
         */
        set(key, value) {
            try {
                localStorage.setItem(`imaginary-${key}`, JSON.stringify(value));
            } catch (err) {
                console.error('[AppState.storage] Write error:', err);
            }
        }
    };

    // =========================================================================
    // TRANSACTION SYSTEM
    // =========================================================================

    /** @type {number} Monotonic transaction epoch counter */
    let _txEpoch = 0;

    /** @type {boolean} Whether we're inside a transaction */
    let _inTransaction = false;

    /** @type {Set<Object>} Domains that need notification after transaction */
    let _dirtyDomains = new Set();

    /** @type {Promise} Queue for serializing async operations */
    let _transactionQueue = Promise.resolve();

    /**
     * Mark a domain as needing notification.
     *
     * If inside a transaction, the notification is deferred until the
     * transaction completes. If outside a transaction, notifies immediately
     * with a warning (mutations should happen in transactions).
     *
     * @param {Object} domain - Domain object with _name and _notify
     */
    function markDirty(domain) {
        if (_inTransaction) {
            _dirtyDomains.add(domain);
        } else {
            console.warn(`[AppState] Mutation outside transaction: ${domain._name}`);
            domain._notify({ type: 'changed', epoch: _txEpoch });
        }
    }

    /**
     * Flush notifications for all dirty domains.
     * Called at end of transaction.
     * @private
     */
    function flushDirty() {
        const domains = Array.from(_dirtyDomains);
        _dirtyDomains.clear();

        if (DEBUG && domains.length > 0) {
            console.log(`[AppState.transaction] Flushing ${domains.length} dirty domains:`,
                domains.map(d => d._name).join(', '));
        }

        for (const domain of domains) {
            domain._notify({ type: 'changed', epoch: _txEpoch });
        }
    }

    /**
     * Run a function within a synchronous transaction.
     *
     * All state mutations should happen inside transactions. The transaction:
     * 1. Batches all markDirty() calls
     * 2. Increments the epoch counter
     * 3. Notifies all dirty domains once at the end
     *
     * Transactions can be nested - only the outermost transaction flushes.
     *
     * @param {Function} fn - Synchronous function to run
     * @returns {*} Result of fn
     *
     * @example
     * transaction(() => {
     *     faces._internal.linkToPerson(faceId, personId, name);
     *     people._internal.incrementFaceCount(personId);
     * });
     * // Both domains notified once here
     */
    function transaction(fn) {
        if (_inTransaction) {
            // Nested transaction - just run, outer transaction will flush
            return fn();
        }

        _inTransaction = true;
        _txEpoch++;
        _dirtyDomains.clear();

        if (DEBUG) {
            console.log(`[AppState.transaction] BEGIN epoch=${_txEpoch}`);
        }

        try {
            const result = fn();
            flushDirty();
            return result;
        } finally {
            _inTransaction = false;
            if (DEBUG) {
                console.log(`[AppState.transaction] END epoch=${_txEpoch}`);
            }
        }
    }

    /**
     * Queue an async operation to run after pending operations complete.
     *
     * Used for API calls that should be serialized. The queue ensures
     * operations complete in order even if they have different latencies.
     *
     * @param {Function} fn - Async function to queue
     * @returns {Promise} Resolves when fn completes
     *
     * @example
     * return queueTransaction(async () => {
     *     await App.apiPost('/faces/assign', { face_ids, person_id });
     * });
     */
    function queueTransaction(fn) {
        // Create a new promise that captures this transaction's result
        // but doesn't break the queue chain if it fails
        let resolve, reject;
        const resultPromise = new Promise((res, rej) => {
            resolve = res;
            reject = rej;
        });

        // Chain onto queue, but always resolve the queue chain
        // (individual transaction errors go to resultPromise, not the queue)
        _transactionQueue = _transactionQueue
            .then(() => fn())
            .then(result => {
                resolve(result);
            })
            .catch(err => {
                console.error('[AppState] Queued transaction failed:', err);
                reject(err);
                // Don't re-throw - let queue continue to next transaction
            });

        return resultPromise;
    }

    /**
     * Check if currently inside a transaction.
     * @returns {boolean}
     */
    function isInTransaction() {
        return _inTransaction;
    }

    /**
     * Get the current transaction epoch.
     * @returns {number}
     */
    function getTransactionEpoch() {
        return _txEpoch;
    }

    // =========================================================================
    // DEBUG HELPERS
    // =========================================================================

    /** Debug utilities exposed on window for console debugging */
    window._appStateDebug = {
        getEpoch: () => _txEpoch,
        isInTransaction: () => _inTransaction,
        getDirtyDomains: () => Array.from(_dirtyDomains).map(d => d._name),
        enableLogging: () => { /* Would need closure modification */ },
    };

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        // Utilities for domain creation
        createSubscriberSystem,
        storage,

        // Transaction system
        transaction,
        queueTransaction,
        markDirty,
        isInTransaction,
        getTransactionEpoch,

        // Logging helper
        log,

        // Domains will be added by subsequent scripts:
        // view, nav, filter, status, search, folders,
        // images, faces, people, duplicates, selection
    };

})();
