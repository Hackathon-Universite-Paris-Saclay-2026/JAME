===SPECS_START===
**Architecture Specification Document**

# Todo Application System Architecture

## 1. Architectural Overview
Three-tier architecture with microservices for scalability and GDPR compliance:
- **Presentation Tier**: React SPA with PWA support
- **Application Tier**: 
  - API Gateway (Nginx)
  - Auth Service (OAuth2/OIDC)
  - Todo Service (Spring Boot)
  - User Service
  - Audit Service
  - Notification Service
- **Data Tier**:
  - PostgreSQL (ACID-compliant)
  - Redis 7.0+ caching
  - RabbitMQ 3.11+ for event streaming
  - S3-compatible object storage

## 2. Key Quality Attributes
- **Security**: TLS 1.3 everywhere, AES-256 encryption at rest
- **Performance**: 100ms P99 latency, Redis cache with 5ms TTL
- **Reliability**: 95% SLA, k8s liveness/readiness probes
- **GDPR Compliance**: Pseudonymization, 30-day backup purge cycles

## 3. Updated Component Diagram
```plantuml
@startuml
!include C4_Container.puml

Container(web_app, "Web App", "React/PWA", "Provides UI for todo management")
Container(spa_gateway, "API Gateway", "Nginx", "Routes requests and manages CORS")
Container(auth_service, "Auth Service", "Spring Boot", "Handles OAuth2/OIDC flows and MFA")
Container(todo_service, "Todo Service", "Spring Boot", "Manages todo CRUD operations")
Container(user_service, "User Service", "Spring Boot", "Manages user profiles and RBAC")
Container(audit_service, "Audit Service", "Python", "Processes audit logs from message queue")
Container(notification_service, "Notification Service", "Node.js", "Sends email reminders")

ContainerDb(postgres, "PostgreSQL", "Database", "Stores todos, users, auth data")
ContainerDb(redis, "Redis", "Cache", "Stores session data and todo list cache")
ContainerQueue(rabbitmq, "RabbitMQ", "Message Broker", "Handles audit and notification events")
ContainerDb(s3, "Object Storage", "S3-compatible", "Stores encrypted audit logs")

Rel(web_app, spa_gateway, "HTTPS", "API calls")
Rel(spa_gateway, auth_service, "HTTPS", "Auth requests")
Rel(spa_gateway, todo_service, "HTTPS", "Todo API")
Rel(spa_gateway, user_service, "HTTPS", "User management")

Rel(auth_service, postgres, "JDBC", "User credentials")
Rel(todo_service, postgres, "JDBC", "Todo records")
Rel(todo_service, redis, "RESP", "Cache todos")
Rel(user_service, postgres, "JDBC", "User profiles")
Rel(user_service, redis, "RESP", "Session cache")

Rel(audit_service, rabbitmq, "AMQP", "Consumes audit events")
Rel(audit_service, s3, "S3 API", "Stores encrypted logs")
Rel(notification_service, rabbitmq, "AMQP", "Consumes reminder events")

Rel_R(auth_service, rabbitmq, "AMQP", "Publishes auth events")
Rel_R(todo_service, rabbitmq, "AMQP", "Publishes todo changes")
Rel_R(user_service, rabbitmq, "AMQP", "Publishes profile updates")
@enduml
```

## 4. Enhanced API Specifications (OpenAPI 3.0)
```yaml
paths:
  /todos:
    post:
      security:
        - oauth2: [user]
      # ... existing todo endpoints
  
  /admin/todos:
    get:
      security:
        - oauth2: [admin]
      description: Get all todos (admin only)
      responses:
        200:
          description: List of all todos

  /admin/users/{id}:
    delete:
      security:
        - oauth2: [admin]
      description: Delete user and all associated data
      parameters:
        - name: id
          in: path
          required: true
          schema: {type: string}
```

## 5. GDPR Compliance Updates
**Right to Erasure Implementation:**
1. Data deletion triggers cascade delete across todos, audit logs, and user records
2. Backup purge process:
   - Nightly backup verification jobs
   - Cryptographic shredding of deleted user data in backups
   - S3 object lifecycle policy for audit log rotation (30d retention)

**Audit Logging:**
- Dual storage strategy:
  1. Hot storage: PostgreSQL for real-time queries
  2. Cold storage: Encrypted S3 objects for long-term retention
- Immutable log design with hash chains for integrity verification

## 6. Security Controls
- **RBAC Matrix**:
  | Role       | Todo Access          | User Management |
  |------------|----------------------|-----------------|
  | User       | CRUD own             | None            |
  | Admin      | CRUD all             | Full access     |

- **MFA Flow**:
  1. TOTP setup during registration
  2. Mandatory MFA for admin operations
  3. Backup codes stored with Argon2 hashing

## 7. Deployment Architecture
- **Containerization**: Docker images with distroless base
- **Orchestration**: Kubernetes with:
  - Horizontal pod autoscaling
  - Redis cluster with sentinel
  - PostgreSQL HA with Patroni
  - S3-compatible storage (MinIO)
- **Monitoring**: Prometheus/Grafana with SLA dashboards
===DIAGRAMS_START===
```plantuml
@startuml
!include C4_Container.puml

title Container Diagram for Todo Application

Person(user, "End User", "Interacts with the todo app via web browser")
Person(admin, "Admin User", "Manages users and system settings")

System_Boundary(app, "Todo Application System") {
    Container(web_app, "Web App", "React/PWA", "Client-side application")
    Container(spa_gateway, "API Gateway", "Nginx", "Reverse proxy and rate limiting")
    
    Container(auth_service, "Auth Service", "OAuth2 Provider", "Handles authentication and RBAC")
    Container(todo_service, "Todo Service", "Spring Boot", "Manages todo items")
    Container(user_service, "User Service", "Spring Boot", "Manages user accounts")
    Container(audit_service, "Audit Service", "Python", "Processes audit logs")
    Container(notification_service, "Notification Service", "Node.js", "Sends email reminders")
    
    ContainerDb(postgres, "PostgreSQL", "Database", "Stores todos, users, and auth data")
    ContainerDb(redis, "Redis", "Cache", "Session storage and todo cache")
    ContainerQueue(rabbitmq, "RabbitMQ", "Message Broker", "Handles audit and notification events")
    ContainerDb(s3, "Object Storage", "S3-compatible", "Stores encrypted audit logs")
}

Rel(user, web_app, "HTTPS", "Uses")
Rel(admin, web_app, "HTTPS", "Manages")

Rel(web_app, spa_gateway, "HTTPS", "API calls")
Rel(spa_gateway, auth_service, "HTTPS", "OAuth2 token validation")
Rel(spa_gateway, todo_service, "HTTPS", "Todo API requests")
Rel(spa_gateway, user_service, "HTTPS", "User management API")

Rel(auth_service, postgres, "JDBC", "Stores user credentials")
Rel(auth_service, redis, "RESP", "Session tokens")

Rel(todo_service, postgres, "JDBC", "Persists todos")
Rel(todo_service, redis, "RESP", "Caches frequent todo lists")

Rel(user_service, postgres, "JDBC", "Manages user profiles")
Rel(user_service, redis, "RESP", "Caches user data")

Rel(audit_service, rabbitmq, "AMQP", "Consumes audit events")
Rel(audit_service, s3, "S3 API", "Archives logs")

Rel(notification_service, rabbitmq, "AMQP", "Consumes notification events")

Rel_R(todo_service, rabbitmq, "AMQP", "Publishes todo changes")
Rel_R(user_service, rabbitmq, "AMQP", "Publishes user events")
Rel_R(auth_service, rabbitmq, "AMQP", "Publishes auth events")

LAYOUT_WITH_LEGEND()
@enduml
```