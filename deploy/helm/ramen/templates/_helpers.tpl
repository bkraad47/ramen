{{- define "ramen.labels" -}}
app.kubernetes.io/name: ramen-console
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
{{- define "ramen.selector" -}}
app.kubernetes.io/name: ramen-console
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "ramen.image" -}}
{{- if eq .Values.provider "aws" -}}
{{ printf "%s:%s" (default (printf "%s.dkr.ecr.%s.amazonaws.com/ramen/console" (.Values.aws.account | toString) .Values.region) .Values.image.repository) (.Values.image.tag | toString) }}
{{- else -}}
{{ printf "%s:%s" (default (printf "%s-docker.pkg.dev/%s/ramen/console" .Values.region .Values.project) .Values.image.repository) (.Values.image.tag | toString) }}
{{- end -}}
{{- end -}}
{{- define "ramen.roleArn" -}}
{{ default (printf "arn:aws:iam::%s:role/ramen/ramen-console" (.Values.aws.account | toString)) .Values.aws.roleArn }}
{{- end -}}
{{- define "ramen.gsa" -}}
{{ default (printf "ramen-console@%s.iam.gserviceaccount.com" .Values.project) .Values.serviceAccount.gsa }}
{{- end -}}
{{- define "ramen.secretName" -}}
{{ default "ramen-console" .Values.console.existingSecret }}
{{- end -}}
