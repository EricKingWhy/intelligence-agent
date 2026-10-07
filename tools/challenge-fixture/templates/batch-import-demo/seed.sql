-- 7 条旧记录：ID 和 content 固定，hash 由 judge 按 sha256(content) 校验
-- 挑战期间不得删除或改写（用户约束 #1）
INSERT OR IGNORE INTO records (id, payload_hash, content) VALUES
    ('seed-001', 'placeholder', 'legacy customer alpha'),
    ('seed-002', 'placeholder', 'legacy customer beta'),
    ('seed-003', 'placeholder', 'legacy customer gamma'),
    ('seed-004', 'placeholder', 'legacy order 1001'),
    ('seed-005', 'placeholder', 'legacy order 1002'),
    ('seed-006', 'placeholder', 'legacy invoice A-1'),
    ('seed-007', 'placeholder', 'legacy invoice A-2');
