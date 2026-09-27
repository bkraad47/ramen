{{- define "rw.ns" -}}{{ printf "ramen-%s-%s" .Values.group .Values.zone }}{{- end -}}
{{- define "rw.labels" -}}
app.kubernetes.io/name: ramen-worker
app.kubernetes.io/version: {{ .Values.tag | quote }}
ramen.io/group: {{ .Values.group | quote }}
ramen.io/zone: {{ .Values.zone | quote }}
{{- end -}}
{{- define "rw.image" -}}
{{ default (printf "%s-docker.pkg.dev/%s/ramen/worker:%s" .Values.region .Values.project (.Values.tag | toString)) .Values.image }}
{{- end -}}
{{- define "rw.gsa" -}}
{{ default (printf "ramen-%s-%s@%s.iam.gserviceaccount.com" .Values.group .Values.zone .Values.project) .Values.serviceAccount.gsa }}
{{- end -}}
{{- define "rw.bucketUri" -}}
{{ default (printf "gs://ramen-%s-groups/%s" .Values.project .Values.group) .Values.bucketUri }}
{{- end -}}
