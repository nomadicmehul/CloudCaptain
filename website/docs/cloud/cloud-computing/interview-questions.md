---
title: "Cloud Computing Interview Questions"
description: "40+ interview questions with detailed answers for cloud computing roles"
sidebar_label: "Interview Questions"
sidebar_position: 4
---

# Cloud Computing Interview Questions

## Fundamentals

### Q1: Explain the difference between IaaS, PaaS, and SaaS.

**Answer:**

**IaaS (Infrastructure as a Service):**
- Provides virtualized computing resources over the internet
- You manage: Applications, data, runtime, middleware, OS
- Provider manages: Virtualization, servers, storage, networking
- Examples: AWS EC2, Azure VMs, Google Compute Engine
- Best for: Maximum control and flexibility

**PaaS (Platform as a Service):**
- Provides a platform for developing and deploying applications
- You manage: Applications and data
- Provider manages: Everything else (OS, middleware, runtime, infrastructure)
- Examples: Heroku, AWS Elastic Beanstalk, Google App Engine
- Best for: Developers who want to focus on code

**SaaS (Software as a Service):**
- Delivers ready-to-use applications via web browser
- You manage: Nothing
- Provider manages: Everything
- Examples: Salesforce, Microsoft 365, Slack, Google Workspace
- Best for: End users, not infrastructure focused

**Key difference:** Control vs. convenience trade-off

---

### Q2: What is the shared responsibility model?

**Answer:**

The shared responsibility model defines what cloud provider and customer are each responsible for securing.

**General principle:**
- Provider always manages: Physical security, power, cooling, hardware
- Customer always manages: Data, encryption, access control
- Shared based on service model:

**IaaS:**
```
Customer: App, Data, Runtime, Middleware, OS
Provider: Virtualization, Servers, Storage, Networking
```

**PaaS:**
```
Customer: App, Data
Provider: Runtime, Middleware, OS, Virtualization, Servers, Storage
```

**SaaS:**
```
Customer: Data, Access Management
Provider: Everything else
```

**Why it matters:** Misunderstanding shared responsibility leads to security gaps. Always clarify what you're responsible for with each service.

---

### Q3: Explain cloud elasticity vs. scalability.

**Answer:**

**Scalability:**
- Ability to handle increased load by adding resources
- Can be vertical (scale up: more CPU/memory) or horizontal (scale out: more servers)
- Manual process typically

**Elasticity:**
- Ability to automatically scale resources up or down based on demand
- Automatically adjusts capacity
- More sophisticated than scalability
- Key cloud benefit

**Key difference:** Scalability is the capability to scale; elasticity is automatic scaling.

**Example:**
- A website that can handle 1,000 concurrent users (scalable)
- But automatically scales from 100 servers at 1 AM to 5,000 servers at 1 PM (elastic)

---

### Q4: What are regions and availability zones?

**Answer:**

**Regions:**
- Geographic area where cloud provider operates datacenters
- Typically dozens of kilometers apart
- Complete isolation from other regions
- Different pricing per region
- Why choose specific region:
  - Latency (closer to users)
  - Compliance (data residency laws)
  - Service availability (not all services in all regions)

**Availability Zones (AZs):**
- One or more physically separate datacenters within a region
- Isolated power, cooling, networking
- Low-latency networking between AZs (`< 2ms` typically)
- Enable high availability within a region

**Best practice:** Distribute critical applications across multiple AZs for high availability.

---

### Q5: What is the pay-as-you-go model and its benefits?

**Answer:**

**What is it:**
Cloud customers pay only for resources consumed, with no upfront commitment.

**Benefits:**

1. **Reduced capital expenditure:** No need to buy servers upfront
2. **Cost flexibility:** Costs scale with business growth
3. **Faster ROI:** Only pay for what you use
4. **Reduced risk:** Don't get stuck with unused capacity
5. **Easier budgeting:** Operational expense vs. capital expense

**Example:**
- Old way: Buy 50 servers for `$50,000`, use only 10 most of the time
- Cloud way: Pay only for 10 servers most days, scale to 50 during peaks

**Considerations:**
- Need to monitor and optimize costs (easy to over-provision)
- Reserved instances provide discounts for stable baseline loads

---

## Architecture

### Q6: Describe the 3-tier architecture.

**Answer:**

3-tier architecture separates applications into three logical tiers:

```
┌─────────────────────────┐
│ Presentation Tier       │
│ (Web/Mobile UI)         │
└──────────┬──────────────┘
           │
┌──────────▼──────────────┐
│ Application Tier        │
│ (Business Logic)        │
└──────────┬──────────────┘
           │
┌──────────▼──────────────┐
│ Data Tier              │
│ (Database)             │
└──────────────────────────┘
```

**Presentation Tier:**
- User interface (web, mobile, desktop)
- Handles HTTP requests
- Load balanced across multiple servers
- Stateless for scalability

**Application Tier:**
- Business logic and processing
- Service APIs
- Stateless for horizontal scaling
- Handles requests from presentation tier

**Data Tier:**
- Database and storage
- Persistence layer
- Might have read replicas for scaling reads
- Replication for high availability

**Advantages:**
- Clear separation of concerns
- Each tier scales independently
- Easy to test and maintain
- Familiar and well-understood pattern

**When to use:** Traditional web applications, REST APIs, content management systems

---

### Q7: What are microservices and their advantages/disadvantages?

**Answer:**

**What are microservices:**
Architecture where applications are composed of small, independent services that communicate via APIs.

**Advantages:**

1. **Independent scaling:** Scale individual services based on their load
2. **Technology flexibility:** Different services can use different technologies
3. **Faster deployment:** Change one service without redeploying others
4. **Fault isolation:** One service failure doesn't crash entire system
5. **Team autonomy:** Teams own specific services
6. **Easier to understand:** Small codebase per service

**Disadvantages:**

1. **Distributed system complexity:** Debugging across services is harder
2. **Network latency:** Inter-service calls slower than in-process calls
3. **Data consistency:** Distributed transactions are complex
4. **Operational overhead:** More services to deploy, monitor, and manage
5. **Testing complexity:** End-to-end testing across many services

**When to use:**
- Large, complex applications
- Multiple teams working on same system
- Services with different scaling requirements
- Need for independent deployment

**Best practices:**
- Keep services loosely coupled
- Use API gateways for routing
- Implement circuit breakers for resilience
- Use correlation IDs for distributed tracing

---

### Q8: Explain serverless architecture.

**Answer:**

**What is serverless:**
Event-driven architecture where cloud provider manages all infrastructure and you pay only for code execution.

**How it works:**
```
Event (API call, file upload, scheduled time)
    ↓
Trigger Function (Lambda, Cloud Function)
    ↓
Execute Code
    ↓
Return Result
```

**You manage:** Code
**Provider manages:** Servers, scaling, execution environment, infrastructure

**Advantages:**

1. **No server management:** No OS patching, capacity planning, or deployment
2. **Automatic scaling:** Scales from zero to thousands of concurrent executions
3. **Pay per execution:** Only pay when code runs (true pay-as-you-go)
4. **Fast deployment:** Deploy new function in seconds
5. **Built-in high availability:** Geographic distribution, fault tolerance

**Disadvantages:**

1. **Cold starts:** First invocation slower (a few hundred milliseconds)
2. **Execution limits:** Typically 15-minute max execution time
3. **Stateless:** No persistent local storage between invocations
4. **Vendor lock-in:** Each provider has proprietary functions
5. **Debugging:** Harder to debug distributed, event-driven code

**Use cases:**
- Image processing when uploaded
- Scheduled tasks (backups, reports)
- Webhooks and integrations
- APIs with variable traffic
- Real-time data processing
- Asynchronous processing

**Examples:** AWS Lambda, Google Cloud Functions, Azure Functions

---

### Q9: What is load balancing and why is it important?

**Answer:**

**What is load balancing:**
Distributing incoming traffic across multiple servers to prevent any one server from becoming overloaded.

```
        Incoming Traffic
               │
        ┌──────▼──────┐
        │Load Balancer│
        └──────┬──────┘
         ┌─────┼─────┐
         │     │     │
      ┌──▼┐ ┌─▼──┐ ┌─▼──┐
      │Web│ │Web │ │Web │
      │S1 │ │S2  │ │S3  │
      └───┘ └────┘ └────┘
```

**Why important:**

1. **High availability:** If one server fails, others still serve traffic
2. **Scalability:** Add more servers to handle more load
3. **Performance:** Distributes load evenly for faster responses
4. **No single point of failure:** Users not affected by individual server failures

**Load balancing algorithms:**

| Algorithm | Behavior |
|-----------|----------|
| **Round Robin** | Send request to next server in rotation |
| **Least Connections** | Send to server with fewest active connections |
| **IP Hash** | Same client always goes to same server (sticky sessions) |
| **Weighted** | Servers with higher capacity get more traffic |
| **Random** | Randomly distribute |

**Types of load balancers:**

**Layer 4 (Network Load Balancer):**
- Works at TCP/UDP level
- Ultra-high performance
- Limited routing capabilities

**Layer 7 (Application Load Balancer):**
- Works at HTTP/HTTPS level
- Intelligent routing (by URL path, hostname, headers)
- Good for microservices and modern apps

---

### Q10: Explain auto-scaling.

**Answer:**

**What is auto-scaling:**
Automatically adding or removing compute resources based on demand.

**Types of auto-scaling:**

**1. Horizontal Auto-Scaling:**
- Add/remove servers based on demand
- Most common in cloud
- Better for cloud-native applications

**2. Vertical Auto-Scaling:**
- Increase/decrease resources on existing server
- Less common, requires downtime

**How it works:**

```
1. Monitor metrics (CPU, memory, request count)
   ↓
2. Compare against thresholds
   ↓
3. If exceeds threshold → Scale up (add servers)
   ↓
4. If below threshold → Scale down (remove servers)
   ↓
5. Adjust application to match new capacity
```

**Scaling policies:**

| Policy | When to use |
|--------|-----------|
| **Target tracking** | Scale to maintain specific metric (e.g., 70% CPU) |
| **Step scaling** | Different actions based on how much metric exceeds threshold |
| **Scheduled scaling** | Pre-planned scaling (e.g., scale up at 9 AM weekdays) |
| **Predictive scaling** | ML predicts future demand and scales proactively |

**Metrics to scale on:**
- CPU utilization
- Memory usage
- Network bandwidth
- Request count
- Custom application metrics

**Best practices:**
1. Scale on application metrics, not just CPU
2. Set reasonable min/max limits
3. Avoid rapid scaling fluctuations ("flapping")
4. Test scaling behavior in non-production first
5. Monitor for cost implications

---

## Migration & Operations

### Q11: Explain the 6 Rs of cloud migration.

**Answer:**

The 6 Rs are strategies for migrating applications to cloud:

**1. Rehost (Lift and Shift)**
- Move application as-is with minimal changes
- Fastest and cheapest migration
- Doesn't optimize for cloud
- Best for: Quick wins, legacy apps

**2. Replatform (Lift, Tinker, Shift)**
- Make targeted optimizations during migration
- Replace components with managed services (e.g., self-managed DB → RDS)
- Moderate effort and cost
- Best for: Apps benefiting from managed services

**3. Refactor/Re-architect**
- Redesign application for cloud
- Significant code changes
- Maximum cloud benefits
- Highest cost and complexity
- Best for: Strategic, long-term applications

**4. Repurchase (Drop and Shop)**
- Replace with different SaaS product
- Migrate data to new system
- Best for: Legacy systems with SaaS equivalents

**5. Retire**
- Decommission unused applications
- Immediate cost and complexity reduction
- Best for: Redundant or outdated systems

**6. Retain (Do Nothing)**
- Keep on-premises for now
- For applications not ready or unable to migrate

---

### Q12: What are the challenges of lift-and-shift migration?

**Answer:**

While lift-and-shift seems simple, it has significant challenges:

**1. Licensing costs**
- Legacy software with per-socket licensing becomes expensive in cloud
- Solution: Negotiate cloud-friendly licensing, use license-included options

**2. Performance degradation**
- On-premises infrastructure designed differently than cloud
- Solution: Test and optimize, consider different instance types

**3. Network design**
- On-premises designed for same datacenter, close coupling
- Cloud has latency between services
- Solution: Redesign for distributed systems, asynchronous communication

**4. Security model mismatch**
- On-premises security often relies on network perimeter
- Cloud needs zero-trust security
- Solution: Implement modern cloud security practices

**5. Higher than expected costs**
- Legacy apps inefficient in cloud (designed for high per-unit cost)
- Over-provisioning to match on-premises capacity
- Licensing and data transfer costs
- Solution: Right-sizing, optimization, reserve instances

**6. Limited cloud benefits**
- Not using auto-scaling, managed services, elasticity
- Solution: Plan for refactoring after initial migration

**Why lift-and-shift often fails:**
- Underestimated complexity
- Unrealistic expectations about cost savings
- Technical debt never addressed
- Team lacks cloud expertise

**Making it successful:**
1. Realistic assessment of migration effort
2. Build cloud skills in team
3. Consider hybrid approach: rehost then replatform
4. Focus on quick wins, learn for future migrations

---

### Q13: Explain the shared responsibility model for security.

**Answer:**

Security in cloud is a shared responsibility. The specific responsibility depends on service model.

**IaaS Security Responsibility:**
```
Customer: App security, data encryption, access control
          Firewall, network ACLs, OS hardening
          Patch management, vulnerability scanning

Provider: Infrastructure security
          Physical datacenter security
          Network infrastructure
          Virtualization layer
```

**PaaS Security Responsibility:**
```
Customer: App security, data encryption
          Input validation, secure coding
          Authentication/authorization

Provider: Runtime, middleware, OS
          Virtualization, infrastructure
          Platform-level patching
```

**SaaS Security Responsibility:**
```
Customer: Data classification
          User access management
          Credentials protection

Provider: Everything else
          App security, infrastructure
          Data encryption, compliance
```

**Key principles:**
1. **Customer always responsible for data:** Even in SaaS, customer responsible for data content and access control
2. **Read the agreement:** Understand specific responsibilities for your service
3. **Assume nothing:** Don't assume provider handles something not explicitly stated
4. **Defense in depth:** Implement security at multiple layers

**Common mistakes:**
- Thinking cloud provider handles all security
- Neglecting access control in SaaS
- Leaving default credentials unchanged
- Not encrypting sensitive data
- Ignoring compliance requirements

---

### Q14: What is disaster recovery and business continuity?

**Answer:**

**Disaster Recovery (DR):**
Process and procedures to recover from major incidents/failures.

**Business Continuity:**
Broader concept ensuring business continues despite disruptions.

**Key metrics:**

**RTO (Recovery Time Objective):**
- How long until system is back up
- Measured in hours, minutes, or seconds
- Business impact: Cost of downtime

**RPO (Recovery Point Objective)**
- How much data loss is acceptable
- Measured in hours, minutes, or point-in-time
- Business impact: Cost of lost data

**DR strategies:**

| Strategy | RTO | RPO | Cost |
|----------|-----|-----|------|
| **Backup & Restore** | Days | Hours-Days | Low |
| **Pilot Light** | Hours | Minutes-Hours | Low-Moderate |
| **Warm Standby** | Minutes | Seconds-Minutes | Moderate |
| **Hot Standby** | Seconds | Real-time | High |

**1. Backup & Restore:**
- Regular backups to different region
- Slow recovery
- Best for: Non-critical systems

**2. Pilot Light:**
- Minimal hot standby in different region
- Scale up when needed
- Best for: Moderate RPO/RTO requirements

**3. Warm Standby:**
- Scaled-down copy running in different region
- Scale up quickly
- Best for: Important systems

**4. Hot Standby (Active-Active):**
- Fully operational copy in different region
- Instant failover
- Best for: Mission-critical systems

**Implementation:**
- Database replication to other regions
- Multi-region applications
- Automated failover mechanisms
- Regular testing and recovery drills

---

## Performance & Optimization

### Q15: How do you optimize cloud costs?

**Answer:**

**Cost optimization strategies:**

**1. Right-sizing**
- Monitor actual resource usage
- Downsize over-provisioned instances
- Tools: CloudWatch (AWS), Monitor (Azure), Cloud Monitoring (GCP)

**2. Reserved instances**
- Commit to 1-3 year terms
- Save 30-70% vs. on-demand
- Good for: Stable baseline load

**3. Spot instances**
- Use unused capacity at 70-90% discount
- Can be terminated by provider
- Good for: Non-critical, interruptible workloads

**4. Auto-scaling**
- Only pay for capacity in use
- Scale down during low-traffic periods

**5. Managed services**
- AWS RDS vs. self-managed database
- Reduced operational overhead
- Better resource utilization by provider

**6. Data transfer optimization**
- Data transfer out is expensive
- Keep related resources in same region
- Use CDN for external delivery
- Minimize cross-region transfers

**7. Cleanup unused resources**
- Delete unattached storage volumes
- Terminate unused instances
- Clean up old snapshots
- Remove unused databases

**Cost optimization tools:**
- AWS Cost Explorer
- Azure Cost Management
- Google Cloud Cost Management
- Third-party tools (CloudHealth, Cloudability)

**FinOps practice:**
- Make costs visible to engineering teams
- Allocate costs by business unit
- Set budgets and alerts
- Regular cost reviews and optimization

---

### Q16: Explain caching and its benefits.

**Answer:**

**What is caching:**
Storing frequently accessed data in fast, temporary storage to avoid repetitive expensive operations.

**Types of caching:**

**1. Application-level caching:**
```
Request
  ↓
Check Cache ← Cache Hit (fast)
  ↓
If miss → Query Database → Update Cache
```
- Examples: Redis, Memcached
- Reduces database load
- Faster response times

**2. CDN (Content Delivery Network) caching:**
- Cache static content at edge locations
- Served from server closest to user
- Reduces latency significantly
- Examples: CloudFront, Azure CDN, Cloud CDN

**3. Database query caching:**
- Cache frequently run queries
- Reduce database load
- Redis, ElastiCache popular choices

**Benefits:**
1. **Improved performance:** Cached data returns in milliseconds vs. seconds
2. **Reduced database load:** Fewer queries hit database
3. **Better scalability:** Can handle more concurrent users
4. **Cost reduction:** Fewer database resources needed

**Caching challenges:**

**Cache invalidation:**
- How to know when cached data is stale
- Strategies: TTL (time-to-live), event-based invalidation, LRU eviction

**Cache consistency:**
- Ensure cache matches source data
- Problem: If source changes, cache might be old
- Solution: Proper invalidation strategy

**Cache stampede:**
- When popular cache entry expires, many requests hit source simultaneously
- Solution: Staggered TTLs, refresh before expiration

**Best practices:**
1. Cache read-heavy data (user profiles, settings)
2. Avoid caching frequently changing data
3. Use appropriate TTLs
4. Monitor cache hit rates
5. Plan cache invalidation strategy

---

## Monitoring & Observability

### Q17: What is observability in cloud systems?

**Answer:**

**Observability:**
Ability to understand internal state of system from external outputs (logs, metrics, traces).

**Three pillars of observability:**

**1. Logs**
- Detailed event records from applications
- What happened and when
- Example: "User login at 14:23:45, failed authentication"

**2. Metrics**
- Quantitative measurements of system behavior
- Time-series data
- Examples: CPU usage, request latency, error rate

**3. Traces**
- Follow single request through entire system
- Show where time is spent
- Useful for debugging distributed systems

**Why observability matters:**
- Cloud systems distributed, complex, hard to debug
- Need visibility into what's happening
- Faster incident response
- Better understanding of system behavior

**Observability vs. Monitoring:**
- **Monitoring:** Check if system is working (alerts, dashboards)
- **Observability:** Understand why system behaves certain way (exploration)

**Tools:**
- Logs: ELK Stack, Splunk, CloudWatch Logs
- Metrics: Prometheus, Grafana, CloudWatch
- Traces: Jaeger, Datadog, New Relic

---

### Q18: How do you monitor cloud applications?

**Answer:**

**Monitoring strategy:**

**1. Metrics to monitor:**
- **Infrastructure:** CPU, memory, disk usage, network
- **Application:** Response time, error rate, request throughput
- **Business:** Conversion rate, user signups, revenue
- **Cost:** Resource utilization, spending trends

**2. Setting up alerts:**
- Alert on threshold violations
- Alert on anomalies (unusual patterns)
- Escalate critical alerts
- Example: "Alert if error rate > 5% for 5 minutes"

**3. Dashboards:**
- Real-time system health view
- Historical trends
- Service-specific dashboards for teams
- Executive dashboards showing business metrics

**4. Logs:**
- Centralize logs from all services
- Search and analyze logs quickly
- Set up log alerts for specific error patterns
- Archive old logs for compliance

**5. Distributed tracing:**
- Follow requests across services
- Identify bottlenecks
- Understand latency
- Debug issues in microservices

**Monitoring best practices:**

1. **Alert on outcomes, not just metrics:**
   - Don't alert on 90% CPU alone
   - Alert on "response time > 1 second"

2. **Avoid alert fatigue:**
   - Too many alerts = ignored alerts
   - Tune thresholds to avoid false positives

3. **Actionable alerts:**
   - Alert should prompt immediate action
   - Include context and potential causes

4. **Regular reviews:**
   - Review metrics and logs regularly
   - Adjust monitoring based on learnings

---

## Advanced Topics

### Q19: Explain multi-cloud strategy.

**Answer:**

**Multi-cloud:**
Using services from multiple cloud providers in one organization.

**Why use multi-cloud:**
1. **Avoid vendor lock-in:** Don't depend on one provider
2. **Best service per provider:** Use AWS for compute, Azure for enterprise services
3. **Geographic redundancy:** Different providers in different regions
4. **Service availability:** Fallback if one provider has issues

**Challenges:**
1. **Complexity:** Different APIs, tools, management interfaces
2. **Operational overhead:** Team needs expertise in multiple clouds
3. **Cost management:** Harder to track and optimize costs
4. **Security:** Different security models per provider

**Making multi-cloud successful:**

**1. Use abstraction layers:**
- Kubernetes for workload portability
- Terraform for infrastructure as code
- Container images run same on any cloud

**2. Standardize tooling:**
- Use cloud-agnostic tools
- Multi-cloud monitoring platforms
- CI/CD pipelines that work across clouds

**3. Clear governance:**
- Define which workloads on which cloud
- Compliance requirements per cloud
- Regular optimization reviews

**4. Avoid unnecessary complexity:**
- Use multi-cloud only when clear benefit
- Start single cloud, expand as needed

**Don't do multi-cloud just for sake of it—understand the trade-offs.**

---

### Q20: What is infrastructure as code (IaC)?

**Answer:**

**Infrastructure as Code:**
Define infrastructure via code rather than manual configuration.

**Benefits:**

1. **Version control:** Infrastructure in git, track all changes
2. **Reproducibility:** Same infrastructure every time
3. **Automation:** Deploy infrastructure automatically
4. **Documentation:** Code documents infrastructure
5. **Testing:** Validate infrastructure before deployment

**Tools:**

| Tool | Cloud Provider | Language |
|------|---|----------|
| CloudFormation | AWS | JSON/YAML |
| Terraform | Multi-cloud | HCL |
| Bicep | Azure | DSL |
| Ansible | Multi-cloud | YAML |
| Pulumi | Multi-cloud | Python/TypeScript |

**Example (Terraform):**
```hcl
resource "aws_instance" "web" {
  ami           = "ami-12345"
  instance_type = "t2.micro"
  tags = {
    Name = "web-server"
  }
}
```

**Best practices:**
1. Store IaC in version control
2. Use consistent, well-organized structure
3. Validate code before deployment
4. Test infrastructure changes
5. Document any manual changes
6. Use modules for reusability

---

## Additional Practice Questions

### Q21-40: Quick Questions

**Q21:** What is API gateway?
- **Answer:** Service that manages API requests, handles authentication, rate limiting, logging

**Q22:** Explain content delivery network (CDN).
- **Answer:** Distributed servers globally serving content from location closest to user

**Q23:** What is virtual private cloud (VPC)?
- **Answer:** Isolated network environment in cloud where you control IP ranges, subnets, routing

**Q24:** Difference between public and private subnets?
- **Answer:** Public has internet access, private doesn't; private more secure for databases

**Q25:** What is containerization?
- **Answer:** Package application with dependencies in container for consistent deployment

**Q26:** Explain Kubernetes.
- **Answer:** Container orchestration platform automating deployment, scaling, management

**Q27:** What is DevOps?
- **Answer:** Practice bridging development and operations for faster, reliable software delivery

**Q28:** Explain CI/CD.
- **Answer:** CI: automated testing on commits; CD: automated deployment to production

**Q29:** What is a container registry?
- **Answer:** Repository storing container images (Docker Hub, ECR, Azure Container Registry)

**Q30:** Difference between vertical and horizontal scaling?
- **Answer:** Vertical: add resources to server; Horizontal: add more servers

**Q31:** What is eventually consistent?
- **Answer:** Data consistency model where all copies of data become consistent eventually

**Q32:** Explain ACID in databases.
- **Answer:** Atomicity (all or nothing), Consistency (valid state), Isolation (concurrent access), Durability (persistent)

**Q33:** What is database replication?
- **Answer:** Copying data from primary to replica databases for redundancy and scale

**Q34:** Explain circuit breaker pattern.
- **Answer:** Prevent cascading failures by stopping requests to failing service temporarily

**Q35:** What is rate limiting?
- **Answer:** Restrict number of requests per user/time to protect service

**Q36:** Explain zero-trust security.
- **Answer:** Security model assuming no trust, verify every access request

**Q37:** What is encryption at rest?
- **Answer:** Encrypt data when stored to protect if storage media stolen

**Q38:** What is encryption in transit?
- **Answer:** Encrypt data while moving across networks (HTTPS, TLS)

**Q39:** Explain DDoS protection.
- **Answer:** Mitigation techniques against distributed denial-of-service attacks

**Q40:** What is role-based access control (RBAC)?
- **Answer:** Grant permissions based on user roles rather than individual users

---

## Tips for Cloud Computing Interviews

1. **Understand the fundamentals:** Strong grasp of IaaS/PaaS/SaaS, regions, AZs is essential

2. **Know your service models:** Understand what you manage vs. provider manages

3. **Think about trade-offs:** Every architectural decision has pros and cons

4. **Use concrete examples:** Reference real-world systems when explaining concepts

5. **Ask clarifying questions:** "When you say scalable, what does that mean? Peak traffic? Concurrent users?"

6. **Think out loud:** Show your reasoning process, not just final answer

7. **Consider the full picture:** Security, cost, performance, maintainability

8. **Admit knowledge gaps:** "I'm not sure, but here's what I would do..." is better than guessing

9. **Stay updated:** Cloud landscape changes quickly; mention recent developments

10. **Practice system design:** Think about designing services end-to-end (architecture → monitoring)

---

## Next Steps

- **Review fundamentals?** See [Cloud Computing Fundamentals](./fundamentals.md)
- **Study architecture?** Check [Cloud Architecture & Design Patterns](./architecture.md)
- **Learn migration strategies?** Read [Cloud Migration Strategies](./migration.md)
