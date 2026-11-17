-- 工具调用留痕：每次 mock 订单/物流查询都记一条，会话证据导出要用。
-- 为什么单独建表而不是塞进 messages.citations：工具调用不是"证据"，
-- 它没有知识库出处，混在一起会让"引用只能来自证据集"这条纪律变模糊。

create table if not exists tool_calls (
    id bigserial primary key,
    session_id bigint not null references sessions (id) on delete cascade,
    message_id bigint references messages (id) on delete set null,
    tool text not null,
    args jsonb not null default '{}'::jsonb,
    status text not null check (status in ('ok', 'not_found', 'timeout', 'error')),
    latency_ms integer,
    result jsonb,
    created_at timestamptz not null default now()
);

create index if not exists idx_tool_calls_session
    on tool_calls (session_id, created_at desc);
create index if not exists idx_tool_calls_tool
    on tool_calls (tool, created_at desc);
