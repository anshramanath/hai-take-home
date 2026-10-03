-- Fake company (only providers and tools touch these)

CREATE TABLE erp_parts (
    part_id TEXT PRIMARY KEY,
    description TEXT,
    on_hand INT,
    daily_usage INT,
    safety_stock INT,
    unit_cost REAL,
    lot_tracked INT
);

CREATE TABLE erp_suppliers (
    supplier_id TEXT PRIMARY KEY,
    name TEXT,
    contact_email TEXT,
    approved INT,
    approved_parts TEXT,
    lead_time_days INT,
    pricing TEXT
);

CREATE TABLE erp_purchase_orders (
    po_id TEXT PRIMARY KEY,
    part_id TEXT,
    supplier_id TEXT,
    qty INT,
    unit_price REAL,
    total_value REAL,
    ordered_date TEXT,
    promised_date TEXT,
    status TEXT,
    created_by TEXT
);

CREATE TABLE erp_production_orders (
    prod_order_id TEXT PRIMARY KEY,
    product TEXT,
    qty INT,
    scheduled_start TEXT,
    scheduled_end TEXT,
    status TEXT,
    line TEXT,
    supervisor_id TEXT,
    components TEXT
);

CREATE TABLE erp_receipts (
    receipt_id TEXT PRIMARY KEY,
    po_id TEXT,
    qty INT,
    received_date TEXT
);

CREATE TABLE erp_lots (
    lot_id TEXT PRIMARY KEY,
    part_id TEXT,
    qty INT,
    status TEXT,
    received_date TEXT,
    hold_reason TEXT,
    hold_placed_by TEXT,
    hold_placed_on TEXT
);

CREATE TABLE erp_lot_allocations (
    lot_id TEXT,
    prod_order_id TEXT,
    qty INT,
    PRIMARY KEY (lot_id, prod_order_id)
);

CREATE TABLE mail_messages (
    message_id TEXT PRIMARY KEY,
    sender TEXT,
    recipients TEXT,
    sent_at TEXT,
    subject TEXT,
    body TEXT
);

CREATE TABLE cal_events (
    event_id TEXT PRIMARY KEY,
    owner TEXT,
    start TEXT,
    end TEXT,
    title TEXT,
    out_of_office INT
);

CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    name TEXT,
    email TEXT,
    role TEXT,
    manager_id TEXT,
    backup_approver_id TEXT,
    scopes TEXT,
    approval_limits TEXT
);

CREATE TABLE notifications (
    notification_id TEXT PRIMARY KEY,
    to_user TEXT,
    from_user TEXT,
    sent_at TEXT,
    subject TEXT,
    body TEXT
);

-- Harness state

CREATE TABLE clock (
    id INT PRIMARY KEY CHECK (id = 1),
    today TEXT
);

CREATE TABLE attention_items (
    item_id TEXT PRIMARY KEY,
    dedupe_key TEXT UNIQUE,
    detector TEXT,
    owner_id TEXT,
    summary TEXT,
    facts TEXT,
    status TEXT,
    created_at TEXT
);

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    item_id TEXT,
    user_id TEXT,
    status TEXT,
    state TEXT,
    created_at TEXT
);

CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY,
    run_id TEXT,
    approver_id TEXT,
    plan_json TEXT,
    plan_hash TEXT,
    status TEXT,
    requested_at TEXT,
    decided_at TEXT,
    decided_by TEXT,
    routed_reason TEXT
);

CREATE TABLE workflow_instances (
    instance_id TEXT PRIMARY KEY,
    run_id TEXT,
    definition TEXT,
    version INT,
    current_step INT,
    state TEXT,
    status TEXT
);

CREATE TABLE scheduled_tasks (
    task_id TEXT PRIMARY KEY,
    run_at TEXT,
    kind TEXT,
    payload TEXT,
    status TEXT,
    created_by_run TEXT
);

CREATE TABLE executed_actions (
    idempotency_key TEXT PRIMARY KEY,
    tool TEXT,
    args TEXT,
    result TEXT,
    executed_at TEXT
);

CREATE TABLE memory_facts (
    fact_id TEXT PRIMARY KEY,
    subject TEXT,
    fact TEXT,
    source_ids TEXT,
    visible_to_scope TEXT,
    created_at TEXT,
    expires_at TEXT
);

CREATE TABLE audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    run_id TEXT,
    actor TEXT,
    event TEXT,
    detail TEXT
);

CREATE TRIGGER audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE not allowed');
END;

CREATE TRIGGER audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: DELETE not allowed');
END;
