-- ============================================================================
-- 《酒馆老板的记仇簿》（Tavern Keeper's Grudge Ledger）
-- OceanBase seekdb 向量 + 全文混合检索与玩家恩怨账本初始化脚本
-- 兼容版本：OceanBase seekdb 5.7.25-OceanBase seekdb-v1.3.0.0+
-- 用法：mysql -h127.0.0.1 -P2881 -uroot test < dify/sql/init.sql
-- ============================================================================

CREATE TABLE IF NOT EXISTS tavern_lore (
    lore_id VARCHAR(64) PRIMARY KEY,
    category VARCHAR(64) NOT NULL,
    title VARCHAR(128) NOT NULL,
    region VARCHAR(128) NOT NULL,
    danger_level VARCHAR(32) NOT NULL,
    reward_gold INT NOT NULL DEFAULT 0,
    keywords VARCHAR(512) NOT NULL,
    content TEXT NOT NULL,
    npc_secret_hint TEXT NOT NULL,
    embedding VECTOR(8) NOT NULL,
    FULLTEXT INDEX idx_lore_fts (title, content, keywords) WITH PARSER ik,
    VECTOR INDEX idx_lore_vec (embedding) WITH (distance=l2, type=hnsw, lib=vsag)
) DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS tavern_players (
    player_id VARCHAR(64) PRIMARY KEY,
    player_name VARCHAR(64) NOT NULL DEFAULT '旅行者',
    scope_id VARCHAR(128) NOT NULL DEFAULT '',
    favorability INT NOT NULL DEFAULT 60,
    attitude_tier VARCHAR(64) NOT NULL DEFAULT '普通酒客（正常接待）',
    drink_multiplier INT NOT NULL DEFAULT 1,
    grudge_count INT NOT NULL DEFAULT 0,
    favor_count INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) DEFAULT CHARSET = utf8mb4;

CREATE TABLE IF NOT EXISTS tavern_grudge_ledger (
    ledger_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    player_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    turn_id INT NOT NULL DEFAULT 1,
    event_type VARCHAR(32) NOT NULL,
    favor_delta INT NOT NULL DEFAULT 0,
    favor_after INT NOT NULL DEFAULT 60,
    summary TEXT NOT NULL,
    pc_scope_id VARCHAR(128) NOT NULL DEFAULT '',
    pc_entry_id VARCHAR(128) NOT NULL DEFAULT '',
    pc_entry_version_id VARCHAR(128) NOT NULL DEFAULT '',
    pc_revision INT NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_ledger_player (player_id),
    FULLTEXT INDEX idx_ledger_fts (summary) WITH PARSER ik
) DEFAULT CHARSET = utf8mb4;

DELETE FROM tavern_lore WHERE lore_id IN (
    'lore_dragon_01', 'lore_ale_02', 'lore_quest_03', 'lore_herb_04', 'lore_crypt_05'
);

INSERT INTO tavern_lore (
    lore_id, category, title, region, danger_level, reward_gold,
    keywords, content, npc_secret_hint, embedding
) VALUES
(
    'lore_dragon_01',
    'monster',
    '迷雾森林的独眼银鳞龙',
    '迷雾森林北麓·断剑祭坛',
    'S级极危',
    500,
    '迷雾森林 银鳞龙 巨龙 龙鳞 宝箱 秘银 吐息 断剑祭坛',
    '迷雾森林深处的断剑祭坛盘踞着独眼银鳞龙奥尔姆，守护着古代矮人秘银宝箱。它惧怕雷鸣草与淬毒弩箭，左眼旧伤是唯一弱点。',
    '只有巴恩老板信任的冒险者（好感度>=70）才会获知：祭坛北侧瀑布后有矮人逃生暗道，可绕开正面龙息直达秘银宝箱；若记仇（好感度<50），巴恩绝不透露暗道。',
    '[0.92,0.85,0.15,0.10,0.40,0.20,0.88,0.12]'
),
(
    'lore_ale_02',
    'tavern_rule',
    '深炉金麦酒与酒馆铁律',
    '碎盾酒馆吧台',
    '酒馆规矩',
    180,
    '深炉金麦酒 麦酒 辣椒粉 恶作剧 酒桶 翻三倍 赔罪 矮人烈酒 蜂蜜烤肉',
    '巴恩老板最引以为傲的是祖传橡木桶酿造的『深炉金麦酒』（售价 5 枚铜币）。酒馆铁律第一条：严禁往酒桶里掺入辣椒粉或怪味调料，违者记入黑名单且酒水价格翻三倍（15 铜币）！',
    '想消掉巴恩老板的恶作剧旧账，必须主动奉上『冰霜蜂蜜烤肉』或『矮人十年陈酿』诚恳赔罪，老板才会恢复原价并解锁隐藏委托。',
    '[0.15,0.20,0.95,0.90,0.35,0.88,0.18,0.92]'
),
(
    'lore_quest_03',
    'quest',
    '失落的星陨铁酒杯委托',
    '灰烬矿坑二层',
    'A级高危',
    320,
    '委托 任务 悬赏 星陨铁酒杯 灰烬矿坑 哥布林 赏金 暗格',
    '三天前灰烬矿坑的哥布林掠夺者偷走了巴恩老板的传家宝『星陨铁酒杯』。夺回酒杯者可获 320 金币重赏，以及一张通往迷雾森林秘银宝库的羊皮卷。',
    '好感度>=70 时，巴恩会额外借出『矮人破甲重弩』并告知哥布林督军在午夜换岗；若处于记仇状态（好感度<50），巴恩会直接拒绝将此高额悬赏交给玩家。',
    '[0.45,0.60,0.30,0.25,0.92,0.75,0.55,0.30]'
),
(
    'lore_herb_04',
    'item',
    '雷鸣草与冰霜蜂蜜烤肉',
    '晨雾河谷与碎盾厨房',
    'B级常规',
    120,
    '雷鸣草 冰霜蜂蜜烤肉 赔罪 礼物 药材 料理 解毒 龙息',
    '晨雾河谷悬崖生长的雷鸣草能麻痹巨龙神经；而碎盾酒馆特制的『冰霜蜂蜜烤肉』香气四溢，是巴恩老板最爱的下酒菜。',
    '携带冰霜蜂蜜烤肉向巴恩赔礼道歉可大幅提升好感度（+45），并获赠两株烘干的雷鸣草。',
    '[0.55,0.48,0.72,0.68,0.50,0.82,0.42,0.65]'
),
(
    'lore_crypt_05',
    'rumor',
    '旧王都地下酒窖的幽灵守卫',
    '旧王都遗迹',
    'A级高危',
    260,
    '传闻 地下酒窖 幽灵 矮人陈酿 秘银钥匙 暗门',
    '旧王都遗迹下方的封存酒窖里藏着三桶矮人百年陈酿，但入口被幽灵守卫封锁，只有吟诵矮人祝酒词才能安全通行。',
    '好感度高时巴恩会教玩家矮人祝酒词真言：“愿深炉之火永不熄灭”。',
    '[0.62,0.52,0.42,0.38,0.78,0.64,0.70,0.44]'
);
