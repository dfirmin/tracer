# Databricks notebook source
# Generic backfill: copies rows between any two tables. Everything comes from widgets.
src = dbutils.widgets.get("source_table")
dst = dbutils.widgets.get("target_table")
cols = dbutils.widgets.get("columns")
cutoff = dbutils.widgets.get("cutoff_dt")

spark.sql(f"INSERT INTO {dst} SELECT {cols} FROM {src} WHERE load_dt < DATE'{cutoff}'")
