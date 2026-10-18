-- Generated from contracts/features.yaml v1.0.0 by data/common/features.py
-- Idempotent: re-running a batch replaces its rows instead of duplicating them.
MERGE `churnops-512010.churnops.features_daily` AS target
USING (
  SELECT
    CAST(customer_id AS STRING) AS customer_id,
    IFNULL(`gender`, 'Female') AS gender,
    IFNULL(`senior_citizen`, 0) AS senior_citizen,
    CASE WHEN IFNULL(`partner`, 'No') = 'Yes' THEN 1 ELSE 0 END AS partner_encoded,
    CASE WHEN IFNULL(`dependents`, 'No') = 'Yes' THEN 1 ELSE 0 END AS dependents_encoded,
    ROUND((CAST(IFNULL(`tenure`, 0) AS FLOAT64) - 0.0) / 100.0, 4) AS tenure_scaled,
    CASE WHEN IFNULL(`phone_service`, 'No') = 'Yes' THEN 1 ELSE 0 END AS phone_service_encoded,
    IFNULL(`multiple_lines`, 'No') AS multiple_lines,
    IFNULL(`internet_service`, 'No') AS internet_service,
    IFNULL(`online_security`, 'No') AS online_security,
    IFNULL(`online_backup`, 'No') AS online_backup,
    IFNULL(`device_protection`, 'No') AS device_protection,
    IFNULL(`tech_support`, 'No') AS tech_support,
    IFNULL(`streaming_tv`, 'No') AS streaming_tv,
    IFNULL(`streaming_movies`, 'No') AS streaming_movies,
    IFNULL(`contract`, 'Month-to-month') AS contract,
    CASE WHEN IFNULL(`paperless_billing`, 'Yes') = 'Yes' THEN 1 ELSE 0 END AS paperless_billing_encoded,
    IFNULL(`payment_method`, 'Electronic check') AS payment_method,
    ROUND((CAST(IFNULL(`monthly_charges`, 62.135) AS FLOAT64) - 61.3232) / 25.45, 4) AS monthly_charges_scaled,
    ROUND(LN(1 + IFNULL(CAST(`total_charges` AS FLOAT64), 0.0)), 4) AS total_charges_log1p,
    churn,
    customer_segment,
    batch_date,
    batch_id,
    trace_id
  FROM `churnops-512010.churnops.raw_events`
  WHERE batch_id = @batch_id
) AS source
ON target.batch_id = source.batch_id AND target.customer_id = source.customer_id
WHEN MATCHED THEN UPDATE SET
  target.gender = source.gender,
  target.senior_citizen = source.senior_citizen,
  target.partner_encoded = source.partner_encoded,
  target.dependents_encoded = source.dependents_encoded,
  target.tenure_scaled = source.tenure_scaled,
  target.phone_service_encoded = source.phone_service_encoded,
  target.multiple_lines = source.multiple_lines,
  target.internet_service = source.internet_service,
  target.online_security = source.online_security,
  target.online_backup = source.online_backup,
  target.device_protection = source.device_protection,
  target.tech_support = source.tech_support,
  target.streaming_tv = source.streaming_tv,
  target.streaming_movies = source.streaming_movies,
  target.contract = source.contract,
  target.paperless_billing_encoded = source.paperless_billing_encoded,
  target.payment_method = source.payment_method,
  target.monthly_charges_scaled = source.monthly_charges_scaled,
  target.total_charges_log1p = source.total_charges_log1p,
  target.churn = source.churn,
  target.customer_segment = source.customer_segment,
  target.batch_date = source.batch_date,
  target.batch_id = source.batch_id,
  target.trace_id = source.trace_id
WHEN NOT MATCHED THEN INSERT (customer_id, gender, senior_citizen, partner_encoded, dependents_encoded, tenure_scaled, phone_service_encoded, multiple_lines, internet_service, online_security, online_backup, device_protection, tech_support, streaming_tv, streaming_movies, contract, paperless_billing_encoded, payment_method, monthly_charges_scaled, total_charges_log1p, churn, customer_segment, batch_date, batch_id, trace_id)
VALUES (source.customer_id, source.gender, source.senior_citizen, source.partner_encoded, source.dependents_encoded, source.tenure_scaled, source.phone_service_encoded, source.multiple_lines, source.internet_service, source.online_security, source.online_backup, source.device_protection, source.tech_support, source.streaming_tv, source.streaming_movies, source.contract, source.paperless_billing_encoded, source.payment_method, source.monthly_charges_scaled, source.total_charges_log1p, source.churn, source.customer_segment, source.batch_date, source.batch_id, source.trace_id);
