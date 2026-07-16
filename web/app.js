// PelosiTracker presentational logic (vanilla HTML/CSS/JS single-page hash routing)

const appRoot = document.getElementById('app-root');

const state = {
    feedOffset: 0,
    feedLimit: 25
};

// Safe DOM element builder helper to satisfy rules (strictly no innerHTML with dynamic data)
function el(tag, attrs = {}, children = []) {
    const element = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
        if (key === 'textContent') {
            element.textContent = value;
        } else if (key === 'className') {
            element.className = value;
        } else if (key === 'disabled') {
            if (value) {
                element.setAttribute('disabled', 'true');
            } else {
                element.removeAttribute('disabled');
            }
        } else if (key.startsWith('on') && typeof value === 'function') {
            element.addEventListener(key.slice(2).toLowerCase(), value);
        } else {
            element.setAttribute(key, value);
        }
    }
    for (const child of children) {
        if (child !== null && child !== undefined && child !== false) {
            if (typeof child === 'string' || typeof child === 'number') {
                element.appendChild(document.createTextNode(child));
            } else {
                element.appendChild(child);
            }
        }
    }
    return element;
}

// Format cent amounts to USD string format (integer math only)
function formatCents(cents) {
    if (cents === null || cents === undefined) return '';
    const dollars = Math.floor(cents / 100);
    const centsPart = cents % 100;
    const centsStr = centsPart.toString().padStart(2, '0');
    const dollarsStr = dollars.toLocaleString('en-US');
    return `$${dollarsStr}.${centsStr}`;
}

// Format min/max cent amounts as space-en-dash-space bounded ranges
function formatRange(amount) {
    if (!amount) return '';
    const minStr = formatCents(amount.min_cents);
    if (amount.max_cents === null || amount.max_cents === undefined) {
        return `${minStr} +`;
    }
    const maxStr = formatCents(amount.max_cents);
    return `${minStr} \u2013 ${maxStr}`; // U+2013 en dash
}

// Fetch helper targeting standard /api/v1/ endpoints
async function apiFetch(path) {
    const response = await fetch(path);
    const data = await response.json();
    if (!response.ok) {
        throw new Error(data.error || `HTTP error! Status: ${response.status}`);
    }
    return data;
}

function showLoading() {
    appRoot.innerHTML = '';
    appRoot.appendChild(el('div', { className: 'loading-state', textContent: 'Loading…' }));
}

function showError(message) {
    appRoot.innerHTML = '';
    appRoot.appendChild(el('div', { className: 'error-state' }, [
        el('h2', { textContent: 'An Error Occurred' }),
        el('p', { textContent: message })
    ]));
}

function updateActiveNav(activeId) {
    const links = document.querySelectorAll('.nav-links a');
    links.forEach(link => {
        if (link.id === activeId) {
            link.classList.add('active');
        } else {
            link.classList.remove('active');
        }
    });
}

// Reuseable trade card rendering function
function createTradeCard(trade) {
    const chamberClass = trade.chamber === 'house' ? 'badge-house' : 'badge-senate';
    const typeClass = `badge-${trade.transaction_type}`;
    
    let assetEl;
    if (trade.ticker) {
        assetEl = el('div', { className: 'card-asset' }, [
            el('a', { href: `#/ticker/${trade.ticker.toUpperCase()}`, className: 'ticker-link', textContent: trade.ticker.toUpperCase() }),
            el('span', { className: 'asset-name', textContent: ` — ${trade.asset_name}` })
        ]);
    } else {
        assetEl = el('div', { className: 'card-asset' }, [
            el('span', { className: 'asset-name-only', textContent: trade.asset_name })
        ]);
    }

    const disclosureDaysEl = trade.days_to_disclosure !== null 
        ? el('span', { className: 'disclosure-chip', textContent: `filed ${trade.days_to_disclosure} day${trade.days_to_disclosure === 1 ? '' : 's'} later` })
        : null;

    const sourceLinkEl = el('a', {
        href: trade.source_url,
        target: '_blank',
        rel: 'noopener noreferrer',
        className: 'source-link',
        textContent: 'source'
    });

    return el('div', { className: 'trade-card' }, [
        el('div', { className: 'card-header' }, [
            el('div', { className: 'politician-info' }, [
                el('a', { href: `#/politician/${trade.politician_id}`, className: 'politician-link', textContent: trade.politician_name }),
                el('span', { className: `badge ${chamberClass}`, textContent: trade.chamber.toUpperCase() })
            ]),
            el('span', { className: `badge ${typeClass}`, textContent: trade.transaction_type.toUpperCase() })
        ]),
        el('div', { className: 'card-body' }, [
            assetEl,
            el('div', { className: 'card-amount', textContent: formatRange(trade.amount) })
        ]),
        el('div', { className: 'card-footer' }, [
            el('div', { className: 'dates-info' }, [
                el('span', { textContent: `Transacted: ${trade.transaction_date}` }),
                el('span', { textContent: `Disclosed: ${trade.disclosure_date}` })
            ]),
            disclosureDaysEl,
            sourceLinkEl
        ])
    ]);
}

// Page View 1: Feed
async function renderFeed() {
    showLoading();
    updateActiveNav('nav-feed');
    
    try {
        const data = await apiFetch(`/api/v1/trades?limit=${state.feedLimit}&offset=${state.feedOffset}`);
        appRoot.innerHTML = '';
        
        const feedHeader = el('div', { className: 'view-header' }, [
            el('h1', { textContent: 'Latest Disclosures' })
        ]);
        
        const cardsContainer = el('div', { className: 'cards-container' });
        
        if (data.trades.length === 0) {
            cardsContainer.appendChild(el('div', { className: 'empty-state', textContent: 'No disclosures found.' }));
        } else {
            for (const trade of data.trades) {
                cardsContainer.appendChild(createTradeCard(trade));
            }
        }
        
        const prevBtn = el('button', {
            className: 'btn-page',
            disabled: state.feedOffset === 0,
            textContent: 'Previous',
            onClick: () => {
                state.feedOffset = Math.max(0, state.feedOffset - state.feedLimit);
                renderFeed();
            }
        });
        
        const nextBtn = el('button', {
            className: 'btn-page',
            disabled: data.trades.length < state.feedLimit,
            textContent: 'Next',
            onClick: () => {
                state.feedOffset += state.feedLimit;
                renderFeed();
            }
        });
        
        const pagination = el('div', { className: 'pagination' }, [
            prevBtn,
            el('span', { className: 'page-info', textContent: `Page ${Math.floor(state.feedOffset / state.feedLimit) + 1}` }),
            nextBtn
        ]);
        
        appRoot.appendChild(feedHeader);
        appRoot.appendChild(cardsContainer);
        appRoot.appendChild(pagination);
        
    } catch (err) {
        showError(`Failed to load feed: ${err.message}`);
    }
}

// Page View 2: Politicians list
async function renderPoliticiansList() {
    showLoading();
    updateActiveNav('nav-politicians');
    
    try {
        const data = await apiFetch('/api/v1/politicians');
        appRoot.innerHTML = '';
        
        const header = el('div', { className: 'view-header' }, [
            el('h1', { textContent: 'Politicians' })
        ]);
        
        const tbody = el('tbody');
        
        if (data.politicians.length === 0) {
            tbody.appendChild(el('tr', {}, [
                el('td', { colSpan: '3', className: 'empty-table-cell', textContent: 'No politicians found.' })
            ]));
        } else {
            for (const p of data.politicians) {
                const nameLink = el('a', { href: `#/politician/${p.id}`, textContent: p.full_name });
                const chamberClass = p.chamber === 'house' ? 'badge-house' : 'badge-senate';
                tbody.appendChild(el('tr', {}, [
                    el('td', {}, [nameLink]),
                    el('td', {}, [
                        el('span', { className: `badge ${chamberClass}`, textContent: p.chamber.toUpperCase() })
                    ]),
                    el('td', { textContent: p.trade_count.toLocaleString() })
                ]));
            }
        }
        
        const table = el('table', { className: 'data-table' }, [
            el('thead', {}, [
                el('tr', {}, [
                    el('th', { textContent: 'Name' }),
                    el('th', { textContent: 'Chamber' }),
                    el('th', { textContent: 'Trade Count' })
                ])
            ]),
            tbody
        ]);
        
        const wrapper = el('div', { className: 'table-responsive' }, [table]);
        
        appRoot.appendChild(header);
        appRoot.appendChild(wrapper);
        
    } catch (err) {
        showError(`Failed to load politicians: ${err.message}`);
    }
}

// Page View 3: Politician details
async function renderPoliticianDetail(id) {
    showLoading();
    updateActiveNav('');
    
    try {
        const [politician, tradesData] = await Promise.all([
            apiFetch(`/api/v1/politicians/${id}`),
            apiFetch(`/api/v1/trades?politician_id=${id}&limit=50`)
        ]);
        
        appRoot.innerHTML = '';
        
        const chamberClass = politician.chamber === 'house' ? 'badge-house' : 'badge-senate';
        const header = el('div', { className: 'view-header-detail' }, [
            el('div', { className: 'title-section' }, [
                el('h1', { textContent: politician.full_name }),
                el('span', { className: `badge ${chamberClass}`, textContent: politician.chamber.toUpperCase() })
            ])
        ]);
        
        const statsContainer = el('div', { className: 'stats-container' }, [
            el('div', { className: 'stat-card' }, [
                el('div', { className: 'stat-label', textContent: 'Trade Count' }),
                el('div', { className: 'stat-value', textContent: politician.trade_count.toLocaleString() })
            ]),
            el('div', { className: 'stat-card' }, [
                el('div', { className: 'stat-label', textContent: 'Total Volume Range' }),
                el('div', { className: 'stat-value-range', textContent: formatRange(politician.total_volume) })
            ])
        ]);
        
        const topTickersList = el('ul', { className: 'top-tickers-list' });
        if (politician.top_tickers.length === 0) {
            topTickersList.appendChild(el('li', { className: 'empty-list-item', textContent: 'No ticker trades found.' }));
        } else {
            for (const t of politician.top_tickers) {
                topTickersList.appendChild(el('li', {}, [
                    el('a', { href: `#/ticker/${t.ticker.toUpperCase()}`, textContent: t.ticker.toUpperCase() }),
                    el('span', { textContent: ` (${t.trades} trade${t.trades === 1 ? '' : 's'})` })
                ]));
            }
        }
        const topTickersSection = el('div', { className: 'detail-section' }, [
            el('h2', { textContent: 'Top 5 Traded Tickers' }),
            topTickersList
        ]);
        
        const tbody = el('tbody');
        if (tradesData.trades.length === 0) {
            tbody.appendChild(el('tr', {}, [
                el('td', { colSpan: '6', className: 'empty-table-cell', textContent: 'No trades found for this politician.' })
            ]));
        } else {
            for (const trade of tradesData.trades) {
                const tickerEl = trade.ticker
                    ? el('a', { href: `#/ticker/${trade.ticker.toUpperCase()}`, textContent: trade.ticker.toUpperCase() })
                    : el('span', { className: 'asset-text', textContent: trade.asset_name });
                const typeClass = `badge-${trade.transaction_type}`;
                const sourceLink = el('a', {
                    href: trade.source_url,
                    target: '_blank',
                    rel: 'noopener noreferrer',
                    className: 'source-link',
                    textContent: 'Link'
                });
                
                tbody.appendChild(el('tr', {}, [
                    el('td', {}, [tickerEl]),
                    el('td', {}, [
                        el('span', { className: `badge ${typeClass}`, textContent: trade.transaction_type.toUpperCase() })
                    ]),
                    el('td', { textContent: formatRange(trade.amount) }),
                    el('td', { textContent: trade.transaction_date }),
                    el('td', { textContent: trade.disclosure_date }),
                    el('td', {}, [sourceLink])
                ]));
            }
        }
        
        const table = el('table', { className: 'data-table' }, [
            el('thead', {}, [
                el('tr', {}, [
                    el('th', { textContent: 'Ticker / Asset' }),
                    el('th', { textContent: 'Type' }),
                    el('th', { textContent: 'Amount Range' }),
                    el('th', { textContent: 'Transaction Date' }),
                    el('th', { textContent: 'Disclosure Date' }),
                    el('th', { textContent: 'Source' })
                ])
            ]),
            tbody
        ]);
        const wrapper = el('div', { className: 'table-responsive' }, [table]);
        
        const tradesTableSection = el('div', { className: 'detail-section' }, [
            el('h2', { textContent: 'Recent Trades (Limit 50)' }),
            wrapper
        ]);
        
        appRoot.appendChild(header);
        appRoot.appendChild(statsContainer);
        appRoot.appendChild(topTickersSection);
        appRoot.appendChild(tradesTableSection);
        
    } catch (err) {
        showError(`Failed to load politician detail: ${err.message}`);
    }
}

// Page View 4: Ticker details
async function renderTickerDetail(symbol) {
    showLoading();
    updateActiveNav('');
    const upperSymbol = symbol.toUpperCase();
    
    try {
        const data = await apiFetch(`/api/v1/tickers/${encodeURIComponent(upperSymbol)}/trades`);
        appRoot.innerHTML = '';
        
        const header = el('div', { className: 'view-header' }, [
            el('h1', { textContent: `Ticker: ${upperSymbol}` })
        ]);
        
        const cardsContainer = el('div', { className: 'cards-container' });
        
        if (data.trades.length === 0) {
            cardsContainer.appendChild(el('div', { className: 'empty-state', textContent: `No disclosures found for ticker ${upperSymbol}.` }));
        } else {
            for (const trade of data.trades) {
                cardsContainer.appendChild(createTradeCard(trade));
            }
        }
        
        appRoot.appendChild(header);
        appRoot.appendChild(cardsContainer);
        
    } catch (err) {
        showError(`Failed to load ticker detail for ${upperSymbol}: ${err.message}`);
    }
}

// Page View 5: Watchlist list (read-only view)
async function renderWatchlists() {
    showLoading();
    updateActiveNav('nav-watchlists');
    
    try {
        const data = await apiFetch('/api/v1/watchlists');
        appRoot.innerHTML = '';
        
        const header = el('div', { className: 'view-header' }, [
            el('h1', { textContent: 'Watchlists' })
        ]);
        
        const tbody = el('tbody');
        
        if (data.watchlists.length === 0) {
            appRoot.appendChild(header);
            appRoot.appendChild(el('div', { className: 'empty-state' }, [
                el('p', { textContent: 'No watchlist entries.' }),
                el('code', { textContent: 'python -m pelositracker watch add --ticker NVDA' })
            ]));
            return;
        }
        
        for (const item of data.watchlists) {
            let targetEl;
            if (item.kind === 'politician') {
                targetEl = el('a', { href: `#/politician/${item.politician_id}`, textContent: item.politician_name || `Politician #${item.politician_id}` });
            } else {
                targetEl = el('a', { href: `#/ticker/${item.ticker.toUpperCase()}`, textContent: item.ticker.toUpperCase() });
            }
            
            tbody.appendChild(el('tr', {}, [
                el('td', { textContent: item.kind }),
                el('td', {}, [targetEl]),
                el('td', { textContent: item.created_at })
            ]));
        }
        
        const table = el('table', { className: 'data-table' }, [
            el('thead', {}, [
                el('tr', {}, [
                    el('th', { textContent: 'Kind' }),
                    el('th', { textContent: 'Target' }),
                    el('th', { textContent: 'Created At' })
                ])
            ]),
            tbody
        ]);
        const wrapper = el('div', { className: 'table-responsive' }, [table]);
        
        appRoot.appendChild(header);
        appRoot.appendChild(wrapper);
        
    } catch (err) {
        showError(`Failed to load watchlist: ${err.message}`);
    }
}

// Client-side Hash Router
function router() {
    const hash = window.location.hash || '#/feed';
    
    if (hash === '#/feed') {
        renderFeed();
    } else if (hash.startsWith('#/politician/')) {
        const id = hash.split('#/politician/')[1];
        if (id) {
            renderPoliticianDetail(id);
        } else {
            renderFeed();
        }
    } else if (hash.startsWith('#/ticker/')) {
        const symbol = hash.split('#/ticker/')[1];
        if (symbol) {
            renderTickerDetail(decodeURIComponent(symbol));
        } else {
            renderFeed();
        }
    } else if (hash === '#/watchlists') {
        renderWatchlists();
    } else if (hash === '#/politicians') {
        renderPoliticiansList();
    } else {
        renderFeed();
    }
}

// Bootstrap (reviewer integration): wire search, react to hash changes, first render
document.getElementById('search-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const input = document.getElementById('search-input');
    const symbol = input.value.trim();
    if (symbol) {
        window.location.hash = `#/ticker/${encodeURIComponent(symbol.toUpperCase())}`;
        input.value = '';
    }
});

window.addEventListener('hashchange', router);
router();