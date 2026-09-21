"""
MeetingsCount on AWS: Fargate + ALB + RDS Postgres + Cognito, us-east-2.

Sized for a county pilot (a few hundred participants). Roughly $56/month at
September 2026 rates; see README.md for the line items and for why App Runner,
a NAT Gateway and Aurora Serverless are all deliberately not here.

Everything that holds personal data is encrypted with a customer managed KMS
key, the database has no public route, and the task runs as a non-root user
(see the Dockerfile).
"""
from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_certificatemanager as acm,
    aws_cognito as cognito,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_kms as kms,
    aws_logs as logs,
    aws_rds as rds,
)
from constructs import Construct


class MeetingsCountStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kw) -> None:
        super().__init__(scope, cid, **kw)

        domain = self.node.try_get_context("domain")          # optional
        cert_arn = self.node.try_get_context("cert_arn")      # optional

        # --- network -----------------------------------------------------
        # Two AZs (the ALB requires it), public + isolated subnets only.
        # No NAT Gateway: it would cost more than the compute. The task sits
        # in a public subnet with a public IP and is reachable only through
        # the load balancer's security group.
        vpc = ec2.Vpc(
            self, "Vpc", max_azs=2, nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC,
                                        cidr_mask=24),
                ec2.SubnetConfiguration(name="db", cidr_mask=24,
                                        subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            ],
        )

        # --- encryption key ----------------------------------------------
        key = kms.Key(
            self, "DataKey", enable_key_rotation=True,
            description="MeetingsCount: database and secrets at rest",
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- database -----------------------------------------------------
        db = rds.DatabaseInstance(
            self, "Db",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_16_3),
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE4_GRAVITON,
                                              ec2.InstanceSize.MICRO),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            allocated_storage=20,
            storage_type=rds.StorageType.GP3,
            storage_encrypted=True,
            storage_encryption_key=key,
            multi_az=False,                       # flip to True before real caseloads
            backup_retention=Duration.days(7),
            deletion_protection=True,
            removal_policy=RemovalPolicy.SNAPSHOT,
            database_name="meetingscount",
            credentials=rds.Credentials.from_generated_secret("mcapp", encryption_key=key),
            cloudwatch_logs_exports=["postgresql"],
        )

        # --- identity -----------------------------------------------------
        # Essentials tier: free to 10,000 monthly users and the tier that
        # carries passkeys and SMS one-time codes. Lite does not have them.
        pool = cognito.UserPool(
            self, "Users",
            self_sign_up_enabled=False,           # officers create participants
            sign_in_aliases=cognito.SignInAliases(email=True, phone=True),
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(sms=True, otp=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12, require_digits=True, require_lowercase=True,
                require_uppercase=True, require_symbols=True),
            account_recovery=cognito.AccountRecovery.EMAIL_AND_PHONE_WITHOUT_MFA,
            removal_policy=RemovalPolicy.RETAIN,
        )
        client = pool.add_client(
            "WebClient",
            auth_flows=cognito.AuthFlow(user_srp=True),
            access_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(14),
            prevent_user_existence_errors=True,
        )
        for group, desc in [("officers", "Probation officers and coordinators"),
                            ("admins", "County administrators")]:
            cognito.CfnUserPoolGroup(self, f"Group{group.title()}",
                                     user_pool_id=pool.user_pool_id,
                                     group_name=group, description=desc)

        # --- the service --------------------------------------------------
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc, container_insights=True)

        env = {
            "MC_ENV": "production",
            "MC_DEMO": "0",                       # no fake timestamps, no reseeding
            "MC_DEV_AUTH": "0",                   # Cognito tokens only
            "MC_COGNITO_REGION": self.region,
            "MC_COGNITO_USER_POOL_ID": pool.user_pool_id,
            "MC_COGNITO_CLIENT_ID": client.user_pool_client_id,
            "MC_PIN_RETENTION_DAYS": "30",
        }

        svc = ecs_patterns.ApplicationLoadBalancedFargateService(
            self, "Service",
            cluster=cluster,
            cpu=512, memory_limit_mib=1024,       # 0.5 vCPU / 1 GB
            desired_count=1,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,     # ~20% cheaper than x86
                operating_system_family=ecs.OperatingSystemFamily.LINUX),
            assign_public_ip=True,                # in lieu of a NAT Gateway
            task_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            public_load_balancer=True,
            redirect_http=bool(cert_arn),
            certificate=(acm.Certificate.from_certificate_arn(self, "Cert", cert_arn)
                         if cert_arn else None),
            domain_name=domain if cert_arn else None,
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_asset("..") ,     # builds the repo Dockerfile
                container_port=8000,
                environment=env,
                # RDS's generated secret holds host/port/username/password as
                # separate fields, so the app composes the URL from them at
                # startup rather than expecting one ready-made.
                secrets={
                    "MC_DB_HOST": ecs.Secret.from_secrets_manager(db.secret, field="host"),
                    "MC_DB_PORT": ecs.Secret.from_secrets_manager(db.secret, field="port"),
                    "MC_DB_USER": ecs.Secret.from_secrets_manager(db.secret, field="username"),
                    "MC_DB_PASSWORD": ecs.Secret.from_secrets_manager(db.secret, field="password"),
                    "MC_DB_NAME": ecs.Secret.from_secrets_manager(db.secret, field="dbname"),
                },
                log_driver=ecs.LogDrivers.aws_logs(
                    stream_prefix="mc",
                    log_retention=logs.RetentionDays.SIX_MONTHS),
            ),
        )

        svc.target_group.configure_health_check(
            path="/healthz", healthy_threshold_count=2, interval=Duration.seconds(30))
        svc.service.connections.allow_to(db, ec2.Port.tcp(5432), "app to postgres")
        db.secret.grant_read(svc.task_definition.task_role)
        key.grant_encrypt_decrypt(svc.task_definition.task_role)

        # Scale on CPU. A pilot will never hit this; it is here so a county
        # demo day does not fall over.
        scaling = svc.service.auto_scale_task_count(min_capacity=1, max_capacity=4)
        scaling.scale_on_cpu_utilization(
            "Cpu", target_utilization_percent=65,
            scale_in_cooldown=Duration.minutes(5),
            scale_out_cooldown=Duration.minutes(1))

        CfnOutput(self, "Url", value=f"https://{domain}" if domain
                  else f"http://{svc.load_balancer.load_balancer_dns_name}")
        CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)
        CfnOutput(self, "DbSecretArn", value=db.secret.secret_arn)
