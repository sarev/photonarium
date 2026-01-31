/**
 * AppState v2 Prototype - Transaction-based state management
 *
 * Key concepts:
 * - External API: What screens call. Wraps operations in transactions.
 * - Internal API: Implementation. Can mark domains dirty, call other internal methods.
 * - Transaction: Tracks dirty domains, batches notifications at end.
 * - No debouncing: Operations run sequentially. GUI batches if needed.
 */

const AppStateV2 = (function() {
    'use strict';

    // =========================================================================
    // TRANSACTION SYSTEM
    // =========================================================================

    let _epoch = 0;
    let _inTransaction = false;
    let _dirtyDomains = new Set();
    let _transactionQueue = Promise.resolve(); // Sequential execution

    /**
     * Mark a domain as needing notification.
     * Called by internal API when state changes.
     */
    function markDirty(domain) {
        if (_inTransaction) {
            _dirtyDomains.add(domain);
        } else {
            // Called outside transaction - warn in dev, notify immediately
            console.warn('State mutation outside transaction:', domain._name);
            domain._notify({ type: 'changed', epoch: _epoch });
        }
    }

    /**
     * Flush notifications for all dirty domains.
     * Called at end of transaction.
     */
    function flushDirty() {
        const domains = Array.from(_dirtyDomains);
        _dirtyDomains.clear();

        // Schedule notifications (allows current call stack to complete)
        if (domains.length > 0) {
            queueMicrotask(() => {
                for (const domain of domains) {
                    domain._notify({ type: 'changed', epoch: _epoch });
                }
            });
        }
    }

    /**
     * Run a function within a transaction.
     * - Tracks dirty domains
     * - Batches notifications at end
     * - Handles sync and async functions
     * - Nested transactions are flattened (inner marks dirty, outer flushes)
     */
    function transaction(fn) {
        // If already in a transaction, just run (nested)
        if (_inTransaction) {
            return fn();
        }

        _inTransaction = true;
        _epoch++;
        _dirtyDomains.clear();

        try {
            const result = fn();

            // Handle async
            if (result && typeof result.then === 'function') {
                return result
                    .then(value => {
                        flushDirty();
                        _inTransaction = false;
                        return value;
                    })
                    .catch(err => {
                        flushDirty(); // Still notify on error - state may have partially changed
                        _inTransaction = false;
                        throw err;
                    });
            }

            // Sync
            flushDirty();
            _inTransaction = false;
            return result;

        } catch (err) {
            flushDirty();
            _inTransaction = false;
            throw err;
        }
    }

    /**
     * Queue a transaction to run after any pending transactions complete.
     * Ensures sequential execution of async operations.
     */
    function queueTransaction(fn) {
        _transactionQueue = _transactionQueue
            .then(() => transaction(fn))
            .catch(err => {
                console.error('Transaction failed:', err);
                throw err;
            });
        return _transactionQueue;
    }

    // =========================================================================
    // SUBSCRIBER SYSTEM
    // =========================================================================

    function createSubscriberSystem() {
        const subscribers = new Set();

        return {
            subscribe(callback) {
                subscribers.add(callback);
                return () => subscribers.delete(callback);
            },
            notify(event) {
                for (const callback of subscribers) {
                    try {
                        callback(event);
                    } catch (err) {
                        console.error('Subscriber error:', err);
                    }
                }
            }
        };
    }

    // =========================================================================
    // DOMAIN FACTORY
    // =========================================================================

    /**
     * Create a domain with internal/external API separation.
     *
     * @param {string} name - Domain name for debugging
     * @param {function} setup - Function that returns { internal, external }
     */
    function createDomain(name, setup) {
        const { subscribe, notify } = createSubscriberSystem();

        // Domain object with notification capability
        const domain = {
            _name: name,
            _notify: notify,
            onChanged: subscribe,
        };

        // Set up internal and external APIs
        const { internal, external } = setup(domain, markDirty);

        // Attach internal API (for use by other domains' internal methods)
        domain._internal = internal;

        // Attach external API (wrapped in transactions)
        for (const [key, value] of Object.entries(external)) {
            if (typeof value === 'function') {
                // Wrap in transaction
                domain[key] = (...args) => queueTransaction(() => value(...args));
            } else {
                // Pass through non-functions (getters, etc.)
                domain[key] = value;
            }
        }

        return domain;
    }

    // =========================================================================
    // EXAMPLE DOMAIN: PEOPLE
    // =========================================================================

    const people = createDomain('people', (domain, markDirty) => {
        let _cache = null;
        let _loading = false;

        const internal = {
            /**
             * Find person by name or create new one.
             * Marks dirty if created.
             */
            async findOrCreate(name) {
                // Ensure loaded
                if (!_cache) await internal.load();

                // Case-insensitive search
                const normalizedName = name.trim().toLowerCase();
                for (const person of _cache.values()) {
                    if (person.name.toLowerCase() === normalizedName) {
                        return person;
                    }
                }

                // Create new person
                const response = await App.apiPost('/people', { name: name.trim() });
                const person = response.data || response;
                _cache.set(person.id, person);
                markDirty(domain);
                return person;
            },

            /**
             * Update person in cache.
             * Marks dirty.
             */
            update(id, changes) {
                const person = _cache?.get(id);
                if (person) {
                    Object.assign(person, changes);
                    markDirty(domain);
                }
            },

            /**
             * Load all people from API.
             * Marks dirty.
             */
            async load() {
                if (_loading) return;
                _loading = true;
                try {
                    const response = await App.apiGet('/people');
                    _cache = new Map(response.map(p => [p.id, p]));
                    markDirty(domain);
                } finally {
                    _loading = false;
                }
            },

            /**
             * Get person by ID (sync, from cache).
             */
            get(id) {
                return _cache?.get(id) || null;
            },
        };

        const external = {
            // Sync getters (no transaction needed, read-only)
            get: (id) => internal.get(id),
            getAll: () => _cache ? Array.from(_cache.values()) : [],
            isLoaded: () => _cache !== null,

            // Async operations (will be wrapped in transaction)
            async load() {
                return internal.load();
            },

            async rename(id, newName) {
                await App.apiPatch(`/people/${id}`, { name: newName });
                internal.update(id, { name: newName });
            },

            async setPreferredFace(personId, faceId) {
                await App.apiPost(`/people/${personId}/set-preferred`, { face_id: faceId });
                internal.update(personId, { preferred_face_id: faceId });
            },
        };

        return { internal, external };
    });

    // =========================================================================
    // EXAMPLE DOMAIN: FACES
    // =========================================================================

    const faces = createDomain('faces', (domain, markDirty) => {
        let _cache = null;
        let _loading = false;

        const internal = {
            async load() {
                if (_loading) return;
                _loading = true;
                try {
                    const response = await App.apiGet('/faces');
                    _cache = new Map(response.map(f => [f.id, f]));
                    markDirty(domain);
                } finally {
                    _loading = false;
                }
            },

            update(id, changes) {
                const face = _cache?.get(id);
                if (face) {
                    Object.assign(face, changes);
                    markDirty(domain);
                }
            },

            get(id) {
                return _cache?.get(id) || null;
            },
        };

        const external = {
            // Sync getters
            get: (id) => internal.get(id),
            getAll: () => _cache ? Array.from(_cache.values()) : [],
            isLoaded: () => _cache !== null,

            // Async operations
            async load() {
                return internal.load();
            },

            /**
             * Identify a face - assigns it to a person (creating if needed).
             * This demonstrates cross-domain internal calls.
             */
            async identify(faceId, name) {
                // Find or create person (uses people's internal API)
                const person = await people._internal.findOrCreate(name);

                // Update face via API
                await App.apiPost(`/faces/${faceId}/identify`, { person_id: person.id });

                // Update local cache
                internal.update(faceId, {
                    person_id: person.id,
                    person_name: person.name,
                });

                // If this is the person's first face, set as preferred
                const personFaces = Array.from(_cache.values())
                    .filter(f => f.person_id === person.id);
                if (personFaces.length === 1) {
                    // Use people's internal API
                    people._internal.update(person.id, { preferred_face_id: faceId });
                    await App.apiPost(`/people/${person.id}/set-preferred`, { face_id: faceId });
                }

                // At end of transaction, both faces AND people domains will notify
                // (if both were marked dirty)
            },

            /**
             * Identify multiple faces at once.
             * GUI batches the user intent, AppState processes as one transaction.
             */
            async identifyMultiple(faceIds, name) {
                const person = await people._internal.findOrCreate(name);

                for (const faceId of faceIds) {
                    await App.apiPost(`/faces/${faceId}/identify`, { person_id: person.id });
                    internal.update(faceId, {
                        person_id: person.id,
                        person_name: person.name,
                    });
                }

                // Set first as preferred if person had no faces before
                // ... (similar logic)
            },

            async suppress(faceId) {
                await App.apiPost(`/faces/${faceId}/suppress`);
                _cache?.delete(faceId);
                markDirty(domain);
            },
        };

        return { internal, external };
    });

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        // Domains
        people,
        faces,

        // Utilities (if needed by external code)
        get epoch() { return _epoch; },

        // For testing/debugging
        _transaction: transaction,
        _queueTransaction: queueTransaction,
    };
})();

// Example usage from GUI:
//
// // Single face identification - one transaction, batched notifications
// await AppStateV2.faces.identify(faceId, 'John');
//
// // Multiple faces - GUI batches, one transaction
// await AppStateV2.faces.identifyMultiple([id1, id2, id3], 'John');
//
// // Subscribe to changes
// AppStateV2.faces.onChanged(({ epoch }) => {
//     console.log('Faces changed at epoch', epoch);
//     renderFaces();
// });
//
// AppStateV2.people.onChanged(() => {
//     renderPeople();
// });
