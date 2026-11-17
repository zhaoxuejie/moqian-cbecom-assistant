-- mock 商业数据：订单 + 物流轨迹。**全部虚构**，用于演示与 B 层评测。
--
-- 覆盖（PRD §4.1 已拍板：5 国 × 3 种异常）：
--   US / UK / DE / FR / ES；异常：清关停滞 customs_hold、丢件 lost、地址错误 wrong_address
-- 单号格式：订单 WB-10001..WB-10015；物流 1Z + 16 位数字。
-- 数据随迁移写入，确定性、可重放；评测直接引用这些单号。

create table if not exists mock_orders (
    order_no text primary key,
    country text not null,
    status text not null check (status in (
        'pending', 'pending_payment', 'unshipped', 'shipped', 'delivered', 'cancelled', 'refunded'
    )),
    payment_status text not null check (payment_status in ('paid', 'pending', 'refunded')),
    address jsonb not null default '{}'::jsonb,
    tracking_no text,
    anomaly text not null default 'none' check (anomaly in (
        'none', 'customs_hold', 'lost', 'wrong_address'
    )),
    updated_at timestamptz not null default now()
);

create table if not exists mock_shipments (
    tracking_no text primary key,
    order_no text not null references mock_orders (order_no) on delete cascade,
    carrier text not null,
    events jsonb not null default '[]'::jsonb,
    current_node text,
    eta date,
    anomaly text not null default 'none' check (anomaly in (
        'none', 'customs_hold', 'lost', 'wrong_address'
    ))
);

insert into mock_orders (order_no, country, status, payment_status, address, tracking_no, anomaly) values
    ('WB-10001', 'US', 'shipped',         'paid',    '{"city": "Austin", "state": "TX", "country": "US"}', '1Z1000100000000001', 'none'),
    ('WB-10002', 'US', 'delivered',       'paid',    '{"city": "Denver", "state": "CO", "country": "US"}', '1Z1000200000000002', 'none'),
    ('WB-10003', 'UK', 'shipped',         'paid',    '{"city": "London", "country": "UK"}',                '1Z1000300000000003', 'customs_hold'),
    ('WB-10004', 'DE', 'delivered',       'paid',    '{"city": "Berlin", "country": "DE"}',                '1Z1000400000000004', 'lost'),
    ('WB-10005', 'FR', 'pending',         'pending', '{"city": "Lyon", "country": "FR"}',                  null,                 'wrong_address'),
    ('WB-10006', 'ES', 'cancelled',       'refunded','{"city": "Madrid", "country": "ES"}',                null,                 'none'),
    ('WB-10007', 'US', 'refunded',        'refunded','{"city": "Seattle", "state": "WA", "country": "US"}','1Z1000700000000007', 'none'),
    ('WB-10008', 'UK', 'delivered',       'paid',    '{"city": "Manchester", "country": "UK"}',            '1Z1000800000000008', 'none'),
    ('WB-10009', 'DE', 'shipped',         'paid',    '{"city": "Munich", "country": "DE"}',                '1Z1000900000000009', 'none'),
    ('WB-10010', 'FR', 'delivered',       'paid',    '{"city": "Paris", "country": "FR"}',                 '1Z1001000000000010', 'none'),
    ('WB-10011', 'ES', 'shipped',         'paid',    '{"city": "Barcelona", "country": "ES"}',             '1Z1001100000000011', 'customs_hold'),
    ('WB-10012', 'US', 'unshipped',       'paid',    '{"city": "Chicago", "state": "IL", "country": "US"}',null,                 'wrong_address'),
    ('WB-10013', 'UK', 'shipped',         'paid',    '{"city": "Bristol", "country": "UK"}',               '1Z1001300000000013', 'lost'),
    ('WB-10014', 'DE', 'pending_payment', 'pending', '{"city": "Hamburg", "country": "DE"}',               null,                 'none'),
    ('WB-10015', 'FR', 'delivered',       'paid',    '{"city": "Nice", "country": "FR"}',                  '1Z1001500000000015', 'none')
on conflict (order_no) do nothing;

insert into mock_shipments (tracking_no, order_no, carrier, events, current_node, eta, anomaly) values
    ('1Z1000100000000001', 'WB-10001', 'USPS Ground Advantage', '[{"at": "2026-10-24", "node": "Label created"}, {"at": "2026-10-26", "node": "Departed Phoenix, AZ"}, {"at": "2026-10-27", "node": "In transit to Austin, TX"}]'::jsonb, 'In transit to Austin, TX', '2026-10-30', 'none'),
    ('1Z1000200000000002', 'WB-10002', 'USPS Ground Advantage', '[{"at": "2026-10-20", "node": "Delivered, Denver, CO"}]'::jsonb, 'Delivered, Denver, CO', '2026-10-20', 'none'),
    ('1Z1000300000000003', 'WB-10003', 'Royal Mail', '[{"at": "2026-10-23", "node": "Departed Leipzig, DE"}, {"at": "2026-10-25", "node": "Held at customs, London"}]'::jsonb, 'Held at customs, London', '2026-11-01', 'customs_hold'),
    ('1Z1000400000000004', 'WB-10004', 'DHL', '[{"at": "2026-10-18", "node": "Delivered, Berlin"}]'::jsonb, 'Carrier marked as lost after delivery scan', '2026-10-18', 'lost'),
    ('1Z1000700000000007', 'WB-10007', 'USPS Ground Advantage', '[{"at": "2026-10-10", "node": "Returned to sender, Seattle, WA"}]'::jsonb, 'Returned to sender', '2026-10-12', 'none'),
    ('1Z1000800000000008', 'WB-10008', 'Royal Mail', '[{"at": "2026-10-21", "node": "Delivered, Manchester"}]'::jsonb, 'Delivered, Manchester', '2026-10-21', 'none'),
    ('1Z1000900000000009', 'WB-10009', 'DHL', '[{"at": "2026-10-25", "node": "Departed Leipzig, DE"}, {"at": "2026-10-27", "node": "In transit to Munich"}]'::jsonb, 'In transit to Munich', '2026-10-29', 'none'),
    ('1Z1001000000000010', 'WB-10010', 'Colissimo', '[{"at": "2026-10-19", "node": "Delivered, Paris"}]'::jsonb, 'Delivered, Paris', '2026-10-19', 'none'),
    ('1Z1001100000000011', 'WB-10011', 'Correos', '[{"at": "2026-10-22", "node": "Departed Madrid"}, {"at": "2026-10-26", "node": "Held at customs, Barcelona"}]'::jsonb, 'Held at customs, Barcelona', '2026-11-03', 'customs_hold'),
    ('1Z1001300000000013', 'WB-10013', 'Royal Mail', '[{"at": "2026-10-20", "node": "Departed Bristol sorting centre"}]'::jsonb, 'Tracker stopped updating 7 days ago', '2026-10-28', 'lost'),
    ('1Z1001500000000015', 'WB-10015', 'Colissimo', '[{"at": "2026-10-17", "node": "Delivered, Nice"}]'::jsonb, 'Delivered, Nice', '2026-10-17', 'none')
on conflict (tracking_no) do nothing;
