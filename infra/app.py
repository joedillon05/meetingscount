#!/usr/bin/env python3
"""CDK entry point:  cdk deploy MeetingsCountStack"""
import aws_cdk as cdk

from meetingscount_stack import MeetingsCountStack

app = cdk.App()
MeetingsCountStack(
    app, "MeetingsCountStack",
    env=cdk.Environment(region=app.node.try_get_context("region") or "us-east-2"),
)
app.synth()
