<#--
  usali login theme — split layout: marketing hero left, auth card right.
  Overrides base's template.ftl wholesale; login.ftl fills the "form"/"info"
  sections. Only the login flow is restyled; other flows (OTP, reset, …)
  inherit this shell too since they all render through registrationLayout.
-->
<#macro registrationLayout bodyClass="" displayInfo=false displayMessage=true displayRequiredFields=false>
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow">
  <title>Open Hospitality — Sign in</title>
  <#if properties.styles?has_content>
    <#list properties.styles?split(' ') as style>
      <link rel="stylesheet" href="${url.resourcesPath}/${style}">
    </#list>
  </#if>
</head>
<body class="usali-login ${bodyClass}">
  <main class="split">

    <!-- ============ left: hero ============ -->
    <section class="hero" aria-hidden="true">
      <div class="hero-scene"></div>
      <div class="hero-scrim"></div>
      <div class="hero-content">
        <span class="hero-badge">
          <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
            <path d="M8 1.5l1.7 1.2 2-.3 1 1.8 1.9.8-.3 2 1.2 1.7-1.2 1.7.3 2-1.9.8-1 1.8-2-.3L8 15.7l-1.7-1.2-2 .3-1-1.8-1.9-.8.3-2L.5 8.5 1.7 6.8l-.3-2 1.9-.8 1-1.8 2 .3L8 1.5z" fill="currentColor" opacity=".35"/>
            <path d="M5.5 8.2l1.8 1.8 3.2-3.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          Open-source hospitality platform
        </span>
        <h1 class="hero-title">The Modern <em>Concierge</em><br>for Global Operations.</h1>
        <p class="hero-copy">Open Hospitality is the open-source operating system for
          modern hotels. Accounting, payroll, employee management, and compliance
          come together in one intelligent workspace, built for service excellence.</p>
      </div>
    </section>

    <!-- ============ right: auth card ============ -->
    <section class="pane">
      <div class="card">
        <div class="brand">
          <span class="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none">
              <rect x="3" y="10" width="18" height="10" rx="1.5" stroke="currentColor" stroke-width="1.6"/>
              <path d="M7 10V7a5 5 0 0 1 10 0v3" stroke="currentColor" stroke-width="1.6"/>
              <path d="M8 14h.01M12 14h.01M16 14h.01" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            </svg>
          </span>
          <span class="brand-words">
            <span class="brand-name">Open Hospitality</span>
            <span class="brand-tag">Open-source hotel operations</span>
          </span>
        </div>

        <div class="card-head">
          <h2 class="card-title"><#nested "header"></h2>
        </div>

        <#if displayMessage && message?has_content && (message.type != 'warning' || !(isAppInitiatedAction??))>
          <div class="alert alert-${message.type}" role="alert">
            <span class="alert-text">${kcSanitize(message.summary)?no_esc}</span>
          </div>
        </#if>

        <#nested "form">

        <#if displayInfo>
          <div class="card-info">
            <#nested "info">
          </div>
        </#if>
      </div>
    </section>

  </main>
</body>
</html>
</#macro>
