-- Databricks notebook source
-- Runs after the nightly load. edw_schema / raw_schema are notebook widgets set by the job.
MERGE INTO ${edw_schema}.claim_fact AS t
USING (
  SELECT claim_id, SUM(adj_amount) AS adj_amount
  FROM ${raw_schema}.claim_adjustment
  WHERE adj_status = 'APPROVED'
  GROUP BY claim_id
) AS a
ON t.claim_id = a.claim_id
WHEN MATCHED THEN UPDATE SET t.paid_amount = t.paid_amount + a.adj_amount;
