locals {
  global_actions = ["iam:*", "sts:*", "organizations:*", "account:*", "support:*", "budgets:*", "cloudfront:*", "route53:*", "health:*"]
  account_actions = [
    "account:*",
    "organizations:*",
    "iam:CreateAccountAlias",
    "iam:DeleteAccountAlias",
    "iam:UpdateAccountPasswordPolicy",
    "iam:DeleteAccountPasswordPolicy",
    "iam:CreateUser",
    "iam:CreateAccessKey",
    "iam:CreateLoginProfile",
    "iam:DeleteRolePermissionsBoundary",
    "iam:PutRolePermissionsBoundary",
  ]
  access_policies = {
    deploy   = data.aws_iam_policy_document.deploy.json
    readonly = data.aws_iam_policy_document.readonly.json
  }
}

data "aws_iam_policy_document" "guardrail" {
  statement {
    sid         = "DenyOutsideAllowedRegions"
    effect      = "Deny"
    not_actions = local.global_actions
    resources   = ["*"]

    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = var.allowed_regions
    }
  }

  statement {
    sid       = "DenyAccountLevelActions"
    effect    = "Deny"
    actions   = local.account_actions
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "boundary" {
  source_policy_documents = [data.aws_iam_policy_document.guardrail.json]

  statement {
    sid       = "MaximumForLandingZoneRoles"
    effect    = "Allow"
    actions   = ["ec2:*", "s3:*", "logs:*", "cloudwatch:*", "iam:Get*", "iam:List*", "sts:GetCallerIdentity"]
    resources = ["*"]
  }

  statement {
    sid       = "PassOnlyLandingZoneRoles"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = ["arn:aws:iam::*:role/${var.name}-*"]
  }
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "ManageNetworkStorageAndLogs"
    effect    = "Allow"
    actions   = ["ec2:*", "s3:*", "logs:*", "cloudwatch:*"]
    resources = ["*"]
  }

  statement {
    sid       = "ReadIdentity"
    effect    = "Allow"
    actions   = ["iam:Get*", "iam:List*", "sts:GetCallerIdentity"]
    resources = ["*"]
  }

  statement {
    sid       = "PassOnlyLandingZoneRoles"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = ["arn:aws:iam::*:role/${var.name}-*"]
  }
}

data "aws_iam_policy_document" "readonly" {
  statement {
    sid    = "ReadOnly"
    effect = "Allow"
    actions = [
      "ec2:Describe*",
      "ec2:Get*",
      "s3:GetBucket*",
      "s3:ListBucket",
      "s3:ListAllMyBuckets",
      "logs:Describe*",
      "logs:Get*",
      "cloudwatch:Describe*",
      "cloudwatch:Get*",
      "cloudwatch:List*",
      "iam:Get*",
      "iam:List*",
      "sts:GetCallerIdentity",
    ]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "trust" {
  statement {
    sid     = "TrustedPrincipals"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = var.trusted_principal_arns
    }
  }
}

resource "aws_iam_policy" "guardrail" {
  name        = "${var.name}-guardrail"
  description = "Explicit denies: regions outside the allow-list and account-level actions"
  policy      = data.aws_iam_policy_document.guardrail.json
  tags        = var.tags
}

resource "aws_iam_policy" "boundary" {
  name        = "${var.name}-boundary"
  description = "Permissions boundary: the most any landing zone role can ever do"
  policy      = data.aws_iam_policy_document.boundary.json
  tags        = var.tags
}

resource "aws_iam_policy" "access" {
  for_each    = local.access_policies
  name        = "${var.name}-${each.key}"
  description = "What the ${each.key} role may do"
  policy      = each.value
  tags        = var.tags
}

resource "aws_iam_role" "this" {
  for_each             = local.access_policies
  name                 = "${var.name}-${each.key}"
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  permissions_boundary = aws_iam_policy.boundary.arn
  max_session_duration = 3600
  tags                 = merge(var.tags, { access = each.key })
}

resource "aws_iam_role_policy_attachment" "access" {
  for_each   = local.access_policies
  role       = aws_iam_role.this[each.key].name
  policy_arn = aws_iam_policy.access[each.key].arn
}

resource "aws_iam_role_policy_attachment" "guardrail" {
  for_each   = local.access_policies
  role       = aws_iam_role.this[each.key].name
  policy_arn = aws_iam_policy.guardrail.arn
}
