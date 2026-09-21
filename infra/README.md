# Infrastructure

Nothing here is deployed yet. It exists so that when there is a pilot date,
standing the environment up is `cdk deploy` rather than a week of clicking.

## What it builds

ECS Fargate behind an Application Load Balancer, RDS Postgres, Cognito, and
Secrets Manager, in `us-east-2` (Ohio: closest region to Michigan).

```
Internet -> ALB (HTTPS, ACM cert) -> Fargate task (this app)
                                        |
                                        +-> RDS Postgres (private subnet)
                                        +-> Secrets Manager (DB password)
                                        +-> Cognito (identity)
```

## Cost, September 2026 rates

| Line | Monthly |
|---|---|
| Fargate 0.5 vCPU / 1 GB, ARM, 24x7 | $14.40 |
| Application Load Balancer (1 LCU floor) | $22.27 |
| RDS db.t4g.micro single-AZ + 20 GB gp3 | $13.98 |
| Public IPv4 address | $3.65 |
| Secrets Manager (1 secret) | $0.40 |
| KMS customer managed key | $1.00 |
| **Total** | **~$56** |

Cognito is $0 at pilot scale: the Essentials tier is free to 10,000 monthly
users and is the tier that includes passkeys and SMS one-time codes. SMS itself
bills through SNS per message.

Three things worth knowing before you build on this:

- **AWS App Runner closed to new customers on 30 April 2026.** It would have
  been the cheaper choice (no load balancer). ECS Express Mode is the
  replacement and has similar ergonomics, but it still provisions an ALB
  underneath, so it does not avoid the $22.
- **No NAT Gateway.** It is $32.85/month per availability zone, more than the
  compute. The task runs in a public subnet with a public IP instead, with
  security groups doing the work. Revisit only if a county's security review
  requires private subnets, and price it in.
- **Not Aurora Serverless v2.** Its 0.5 ACU floor is $43.80/month, more than
  this entire database line. Scale-to-zero exists but costs ~15 seconds on the
  first request after a pause, which is a bad trade for a check-in the user is
  standing there waiting on.

Verify current pricing in the AWS console before committing; these are
September 2026 published rates and they move.

## Deploying, when the time comes

```bash
cd infra
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cdk bootstrap                      # once per account/region
cdk deploy MeetingsCountStack -c domain=app.meetingscount.example
```

Then set the production environment variables on the service:
`MC_ENV=production`, `MC_DEMO=0`, `MC_DEV_AUTH=0`, plus the Cognito pool ids
the stack outputs. The app refuses to boot if demo mode or dev auth is still
on while `MC_ENV=production`.
