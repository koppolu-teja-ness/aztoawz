targetScope = 'resourceGroup'

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-demo'
  location: 'eastus'
  tags: {
    owner: 'platform'
  }
  properties: {
    tenantId: '00000000-0000-0000-0000-000000000000'
    sku: {
      name: 'standard'
      family: 'A'
    }
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: 'func-demo'
  location: 'eastus'
  kind: 'functionapp'
  tags: {
    owner: 'app-team'
  }
  properties: {
    httpsOnly: true
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: 'vnet-demo'
  location: 'eastus'
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.0.0.0/16'
      ]
    }
  }
}

resource subnetA 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' = {
  parent: vnet
  name: 'subnet-a'
  properties: {
    addressPrefix: '10.0.1.0/24'
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stgdemounsupported'
  location: 'eastus'
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {}
}
