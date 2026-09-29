{{- define "rw.ns" -}}{{ printf "ramen-%s-%s" .Values.group .Values.zone }}{{- end -}}
{{- define "rw.labels" -}}
app.kubernetes.io/name: ramen-worker
app.kubernetes.io/version: {{ .Values.tag | quote }}
ramen.io/group: {{ .Values.group | quote }}
ramen.io/zone: {{ .Values.zone | quote }}
{{- end -}}
{{- define "rw.image" -}}
{{- if eq .Values.provider "aws" -}}
{{ default (printf "%s.dkr.ecr.%s.amazonaws.com/ramen/worker:%s" (.Values.aws.account | toString) .Values.aws.region (.Values.tag | toString)) .Values.image }}
{{- else -}}
{{ default (printf "%s-docker.pkg.dev/%s/ramen/worker:%s" .Values.region .Values.project (.Values.tag | toString)) .Values.image }}
{{- end -}}
{{- end -}}
{{- define "rw.az" -}}
{{ ternary .Values.aws.zone .Values.gcpZone (eq .Values.provider "aws") }}
{{- end -}}
{{- define "rw.gsa" -}}
{{ default (printf "ramen-%s-%s@%s.iam.gserviceaccount.com" .Values.group .Values.zone .Values.project) .Values.serviceAccount.gsa }}
{{- end -}}
{{- define "rw.bucketUri" -}}
{{- if eq .Values.provider "aws" -}}
{{ default (printf "s3://ramen-%s-groups/%s" (.Values.aws.account | toString) .Values.group) .Values.bucketUri }}
{{- else -}}
{{ default (printf "gs://ramen-%s-groups/%s" .Values.project .Values.group) .Values.bucketUri }}
{{- end -}}
{{- end -}}
{{/* Trusted x-forwarded-for hops: explicit trustProxyHops, else per provider (the AWS ALB appends "<client>";
     the GCP external ALB appends "<client>, <lb>", so the client is the 2nd entry from the right). */}}
{{- define "rw.trustProxyHops" -}}
{{- if ne (toString .Values.trustProxyHops) "" -}}
{{ .Values.trustProxyHops }}
{{- else -}}
{{ ternary "1" "2" (eq .Values.provider "aws") }}
{{- end -}}
{{- end -}}
