-- Run once against an existing SQLite database before deploying this release.
ALTER TABLE market_snapshots ADD COLUMN spread_pct FLOAT;
ALTER TABLE market_snapshots ADD COLUMN bid_ask_ratio_top5 FLOAT;
ALTER TABLE market_snapshots ADD COLUMN bid_ask_ratio_top10 FLOAT;
ALTER TABLE market_snapshots ADD COLUMN bid_ask_ratio_top25 FLOAT;
ALTER TABLE market_snapshots ADD COLUMN imbalance_top10 FLOAT;
ALTER TABLE market_snapshots ADD COLUMN imbalance_top25 FLOAT;
ALTER TABLE market_snapshots ADD COLUMN largest_bid_wall_distance_pct FLOAT;
ALTER TABLE market_snapshots ADD COLUMN largest_ask_wall_distance_pct FLOAT;
ALTER TABLE market_snapshots ADD COLUMN depth_buy_pressure_score FLOAT;
ALTER TABLE market_snapshots ADD COLUMN depth_sell_pressure_score FLOAT;
ALTER TABLE market_snapshots ADD COLUMN depth_order_book_signal VARCHAR(32);
ALTER TABLE market_snapshots ADD COLUMN depth_reliable BOOLEAN;

ALTER TABLE trade_profiles ADD COLUMN max_spread_pct_for_buy FLOAT NOT NULL DEFAULT 0.50;
ALTER TABLE trade_profiles ADD COLUMN min_depth_bid_ask_ratio_top10_for_buy FLOAT NOT NULL DEFAULT 0.60;
ALTER TABLE trade_profiles ADD COLUMN max_depth_sell_pressure_score_for_buy FLOAT NOT NULL DEFAULT 80.0;
ALTER TABLE trade_profiles ADD COLUMN block_buy_on_strong_sell_pressure BOOLEAN NOT NULL DEFAULT 1;
ALTER TABLE trade_profiles ADD COLUMN block_buy_on_near_ask_wall BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE trade_profiles ADD COLUMN near_wall_distance_pct FLOAT NOT NULL DEFAULT 0.30;
