-- Laniakea Database Schema
-- This file mimics the Dashboard Database structure.
-- Requirements: PostgreSQL instance (Docker recommended).

-- Extension for UUID generation (Optional but recommended for unique identifiers)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Migration Tracking Table (Alembic)
-- Used by SQLAlchemy/Alembic to track the current database version.
CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL,
    PRIMARY KEY (version_num)
);

-- Users Table
-- Stores user profiles synchronized from the Identity Provider (OIDC).
CREATE TABLE users (
    sub VARCHAR(36) NOT NULL,              -- Unique Subject ID from the OIDC provider
    name VARCHAR(128),                     -- Full name
    username VARCHAR(64) NOT NULL,         -- Login username
    given_name VARCHAR(64),
    family_name VARCHAR(64),
    email VARCHAR(64) NOT NULL,
    organisation_name VARCHAR(64),
    picture VARCHAR(128),                  -- Profile picture URL
    role VARCHAR(32) NOT NULL,             -- User role (e.g., admin, user)
    active BOOLEAN NOT NULL DEFAULT TRUE,  -- Account status
    sshkey TEXT,                           -- User's public SSH key
    PRIMARY KEY (sub)
);

-- Groups Table
-- Defines research groups or project-based clusters.
CREATE TABLE users_group (
    name VARCHAR(32) NOT NULL,
    PRIMARY KEY (name)
);

-- Deployments Table
-- Main table for tracking cloud infrastructure requests and lifecycle.
CREATE TABLE deployments (
    uuid VARCHAR(36) NOT NULL,             -- Unique Deployment identifier
    creation_time TIMESTAMP,               -- Time of initial request
    update_time TIMESTAMP,                 -- Last status change time
    physicalId VARCHAR(36),                -- Real ID on the Cloud Provider (OpenStack/AWS UUID)
    description VARCHAR(256),              -- User-friendly name/notes
    status VARCHAR(128),                   -- State (e.g., QUEUED, CREATE_COMPLETE, CREATE_FAILED)
    status_reason TEXT,                    -- Logs or error messages (e.g., Terraform output)
    outputs TEXT,                          -- JSON-formatted output parameters (e.g., IP addresses)
    task VARCHAR(64),                      -- Current orchestration task
    links TEXT,                            -- Associated resource links
    provider_name VARCHAR(128),            -- Target provider (OpenStack, AWS)
    endpoint VARCHAR(256),                 -- Provider API endpoint
    template TEXT,                         -- The underlying Terraform/TOSCA code
    inputs TEXT,                           -- Raw input variables
    params TEXT,                           -- Internal execution parameters
    locked BOOLEAN NOT NULL DEFAULT FALSE, -- Prevents concurrent modifications
    feedback_required BOOLEAN NOT NULL DEFAULT FALSE,
    remote BOOLEAN NOT NULL DEFAULT FALSE,
    issuer VARCHAR(256),                   -- Token issuer (OIDC)
    storage_encryption BOOLEAN NOT NULL DEFAULT FALSE,
    vault_secret_uuid VARCHAR(36),         -- Link to HashiCorp Vault credentials
    vault_secret_key TEXT,
    sub VARCHAR(36),                       -- Owner ID (Foreign Key to users table)
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
    user_group VARCHAR(256),               -- Group context for this deployment
    PRIMARY KEY (uuid),
    -- Foreign Key Constraint: Ensures every deployment belongs to an existing user
    CONSTRAINT deployments_ibfk_1 FOREIGN KEY (sub) REFERENCES users (sub)
);

-- Service Visibility Type (Enumerated type)
CREATE TYPE visibility_type AS ENUM ('private', 'public');

-- Service Catalog Table
-- Stores applications or tools available for deployment.
CREATE TABLE service (
    id SERIAL PRIMARY KEY,                 -- Auto-incrementing primary key
    url VARCHAR(128) NOT NULL UNIQUE,      -- Service access URL
    name VARCHAR(128) NOT NULL,
    icon VARCHAR(128) NOT NULL DEFAULT '', -- Icon identifier or path
    description TEXT,
    visibility visibility_type NOT NULL DEFAULT 'private',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Service Access Table (RBAC)
-- Manages permissions: which groups can see/use which services.
CREATE TABLE service_access (
    id SERIAL PRIMARY KEY,
    service_id INTEGER,
    group_id VARCHAR(32),
    -- On Delete Cascade: If a group or service is removed, access rules are deleted automatically
    CONSTRAINT service_access_ibfk_1 FOREIGN KEY (group_id) REFERENCES users_group (name) ON DELETE CASCADE,
    CONSTRAINT service_access_ibfk_2 FOREIGN KEY (service_id) REFERENCES service (id) ON DELETE CASCADE
);
