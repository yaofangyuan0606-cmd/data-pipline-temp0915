-- Run once in Navicat (as root) against the local MySQL 9.x server.
-- Creates the em_qc database and a dedicated user matching .env.example.
CREATE DATABASE IF NOT EXISTS em_qc CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'em_qc'@'localhost' IDENTIFIED BY 'CHANGE_ME';
CREATE USER IF NOT EXISTS 'em_qc'@'127.0.0.1' IDENTIFIED BY 'CHANGE_ME';
GRANT ALL PRIVILEGES ON em_qc.* TO 'em_qc'@'localhost';
GRANT ALL PRIVILEGES ON em_qc.* TO 'em_qc'@'127.0.0.1';
FLUSH PRIVILEGES;
-- Tables are created by the application:  .venv/bin/python -m emqc init-db
