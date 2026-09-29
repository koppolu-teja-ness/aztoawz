param location string = resourceGroup().location
param functionAppName string = 'func-demo'
param storageAccountName string = 'stfuncdemo'

var appInsightsName = '${functionAppName}-appi'

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {}
}

resource functionApp 'Microsoft.Web/sites@2022-09-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp'
  dependsOn: [
    storage
  ]
  properties: {
    serverFarmId: '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Web/serverfarms/plan'
    siteConfig: {
      appSettings: [
        {
          name: 'APPINSIGHTS_NAME'
          value: appInsightsName
        }
      ]
    }
  }
}
