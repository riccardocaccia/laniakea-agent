# NO need for this file
# used to mimic dashboard DB
# if you want to use this config assure you to have a docker running with Posgre-SQL

-- Estensione per gestire i UUID se necessario (opzionale)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Tabella Versioni (Alembic)
CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL,
    PRIMARY KEY (version_num)
);

-- Tabella Utenti
CREATE TABLE users (
    sub VARCHAR(36) NOT NULL,
    name VARCHAR(128),
    username VARCHAR(64) NOT NULL,
    given_name VARCHAR(64),
    family_name VARCHAR(64),
    email VARCHAR(64) NOT NULL,
    organisation_name VARCHAR(64),
    picture VARCHAR(128),
    role VARCHAR(32) NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    sshkey TEXT,
    PRIMARY KEY (sub)
);

-- Tabella Gruppi
CREATE TABLE users_group (
    name VARCHAR(32) NOT NULL,
    PRIMARY KEY (name)
);

-- Tabella Deployments
CREATE TABLE deployments (
    uuid VARCHAR(36) NOT NULL,
    creation_time TIMESTAMP,
    update_time TIMESTAMP,
    physicalId VARCHAR(36),
    description VARCHAR(256),
    status VARCHAR(128),
    status_reason TEXT,
    outputs TEXT,
    task VARCHAR(64),
    links TEXT,
    provider_name VARCHAR(128),
    endpoint VARCHAR(256),
    template TEXT,
    inputs TEXT,
    params TEXT,
    locked BOOLEAN NOT NULL DEFAULT FALSE,
    feedback_required BOOLEAN NOT NULL DEFAULT FALSE,
    remote BOOLEAN NOT NULL DEFAULT FALSE,
    issuer VARCHAR(256),
    storage_encryption BOOLEAN NOT NULL DEFAULT FALSE,
    vault_secret_uuid VARCHAR(36),
    vault_secret_key TEXT,
    sub VARCHAR(36),
    elastic BOOLEAN NOT NULL DEFAULT FALSE,
    updatable BOOLEAN NOT NULL DEFAULT FALSE,
    keep_last_attempt BOOLEAN NOT NULL DEFAULT FALSE,
    stinputs TEXT,
    selected_template TEXT,
    template_parameters TEXT,
    template_metadata TEXT,
    deployment_type VARCHAR(16),
    additional_outputs TEXT,
    stoutputs TEXT,
    template_type VARCHAR(16),
    user_group VARCHAR(256),
    PRIMARY KEY (uuid),
    CONSTRAINT deployments_ibfk_1 FOREIGN KEY (sub) REFERENCES users (sub)
);

-- Tabella Servizi
CREATE TYPE visibility_type AS ENUM ('private', 'public');

CREATE TABLE service (
    id SERIAL PRIMARY KEY,
    url VARCHAR(128) NOT NULL UNIQUE,
    name VARCHAR(128) NOT NULL,
    icon VARCHAR(128) NOT NULL DEFAULT '',
    description TEXT,
    visibility visibility_type NOT NULL DEFAULT 'private',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Tabella Accesso Servizi
CREATE TABLE service_access (
    id SERIAL PRIMARY KEY,
    service_id INTEGER,
    group_id VARCHAR(32),
    CONSTRAINT service_access_ibfk_1 FOREIGN KEY (group_id) REFERENCES users_group (name) ON DELETE CASCADE,
    CONSTRAINT service_access_ibfk_2 FOREIGN KEY (service_id) REFERENCES service (id) ON DELETE CASCADE
);
