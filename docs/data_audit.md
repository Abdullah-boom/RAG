# Data audit

Server version: 7.0.45

## product.products  (166 docs)

| field | types (in sample) | present | PII? |
|---|---|---|---|
| `SKU` | str:166 | 100% |  |
| `__v` | int:166 | 100% |  |
| `_id` | ObjectId:166 | 100% |  |
| `applicableTo` | list:95 | 57% |  |
| `banner` | str:11, NoneType:1 | 6% |  |
| `categoryIds` | list:166 | 100% |  |
| `createdAt` | datetime:166 | 100% |  |
| `description` | str:166 | 100% | yes? |
| `discountId` | ObjectId:15, NoneType:1 | 9% |  |
| `facilities` | list:166 | 100% |  |
| `image` | str:166 | 100% |  |
| `isBestSeller` | bool:126 | 75% |  |
| `isDeal` | bool:166 | 100% |  |
| `menuIds` | list:166 | 100% |  |
| `name` | str:166 | 100% | yes? |
| `orderdedTimes` | int:166 | 100% |  |
| `prepTime` | int:166 | 100% |  |
| `revenue` | int:166 | 100% |  |
| `selectionEnum` | list:166 | 100% |  |
| `updatedAt` | datetime:166 | 100% |  |

Sample (PII-like fields masked):
```
{'_id': '6579d9db1659fb303c233ec2', 'name': '<masked>', 'prepTime': '900', 'selectionEnum': [], 'menuIds': [], 'image': 'products/1725269964504-Cheezious Special.jpg', 'SKU': '123', 'revenue': '0', 'facilities': ['DELIVERY', 'DINE IN', 'PICK UP'], 'isDeal': 'False', 'description': '<masked>', 'orderdedTimes': '0', 'categoryIds': ['6579cd941659fb303c233966'], 'createdAt': '2023-12-13 16:20:43.704000', 'updatedAt': '2026-04-06 14:22:15.283000', '__v': '0', 'isBestSeller': 'False'}
```

## product.categories  (58 docs)

| field | types (in sample) | present | PII? |
|---|---|---|---|
| `__v` | int:58 | 100% |  |
| `_id` | ObjectId:58 | 100% |  |
| `createdAt` | datetime:58 | 100% |  |
| `image` | str:58 | 100% |  |
| `name` | str:58 | 100% | yes? |
| `updatedAt` | datetime:58 | 100% |  |

Sample (PII-like fields masked):
```
{'_id': '6579b8531659fb303c2333c5', 'name': '<masked>', 'image': 'categories/1724240865920-Pastas.jpg', 'createdAt': '2023-12-13 13:57:39.389000', 'updatedAt': '2024-08-21 11:47:45.922000', '__v': '0'}
```

## product.addons  (24 docs)

| field | types (in sample) | present | PII? |
|---|---|---|---|
| `__v` | int:24 | 100% |  |
| `_id` | ObjectId:24 | 100% |  |
| `createdAt` | datetime:24 | 100% |  |
| `type` | str:24 | 100% |  |
| `updatedAt` | datetime:24 | 100% |  |
| `variations` | list:24 | 100% |  |
| `variations[].SKU` | str:24 | 100% |  |
| `variations[].image` | str:24 | 100% |  |
| `variations[].name` | str:24 | 100% | yes? |
| `variations[].price` | int:24 | 100% |  |

Sample (PII-like fields masked):
```
{'_id': '6579d09c1659fb303c233baf', 'type': 'Extra Small Topping', 'variations': [{'SKU': '1', 'image': 'products/addons/1702482076270-download 1-gjx.jpeg', 'price': '120', 'name': '<masked>'}, {'SKU': '2', 'image': 'products/addons/1702482076271-download 2-zrg.jpeg', 'price': '120', 'name': '<masked>'}], 'createdAt': '2023-12-13 15:41:16.273000', 'updatedAt': '2023-12-13 15:41:16.273000', '__v': '0'}
```

## product.discounts  (8 docs)

| field | types (in sample) | present | PII? |
|---|---|---|---|
| `__v` | int:8 | 100% |  |
| `_id` | ObjectId:8 | 100% |  |
| `amount` | int:6, NoneType:2 | 75% |  |
| `applicableOn` | str:8 | 100% |  |
| `branchIds` | list:8 | 100% |  |
| `createdAt` | datetime:8 | 100% |  |
| `endDate` | datetime:8 | 100% |  |
| `endTime` | int:8 | 100% |  |
| `facilities` | list:8 | 100% |  |
| `isAvailable` | bool:8 | 100% |  |
| `maxDiscountAmount` | NoneType:6, int:2 | 75% |  |
| `minOrderAmount` | int:8 | 100% |  |
| `percentage` | NoneType:6, int:2 | 75% |  |
| `platform` | list:8 | 100% | yes? |
| `startDate` | datetime:8 | 100% |  |
| `startTime` | int:8 | 100% |  |
| `type` | str:8 | 100% |  |
| `updatedAt` | datetime:8 | 100% |  |

Sample (PII-like fields masked):
```
{'_id': '65afaf6c4fdc744b6a8c5980', 'branchIds': ['658173d45b6fa94186a15ba3', '6509a6ee57211fb674f1a2c4'], 'type': 'FLAT', 'applicableOn': 'PRODUCT', 'amount': '10', 'percentage': 'None', 'isAvailable': 'False', 'minOrderAmount': '30', 'maxDiscountAmount': 'None', 'facilities': ['Dalivery', 'Take Away', 'Dine In'], 'startTime': '720', 'endTime': '1392', 'startDate': '2024-01-23 12:18:58.503000', 'endDate': '2024-01-24 12:18:58.503000', 'platform': ['<masked>'], 'createdAt': '2024-01-23 12:22:04.627000', 'updatedAt': '2024-01-23 12:27:07.068000', '__v': '0'}
```

## product.branches  (54 docs)

| field | types (in sample) | present | PII? |
|---|---|---|---|
| `__v` | int:54 | 100% |  |
| `_id` | ObjectId:54 | 100% |  |
| `address` | str:54 | 100% | yes? |
| `adminIds` | list:54 | 100% |  |
| `allow24HoursShifts` | bool:48 | 88% |  |
| `autoApprove` | bool:54 | 100% |  |
| `autoCloseTime` | int:6 | 11% |  |
| `autoDispatch` | bool:54 | 100% |  |
| `autoPublishTime` | NoneType:54 | 100% |  |
| `autoReopenTime` | int:38 | 70% |  |
| `bothLegAmount` | int:54 | 100% |  |
| `branchHexLimit` | int:40 | 74% |  |
| `branchHexResolution` | int:40 | 74% |  |
| `branchPrepTime` | int:54 | 100% |  |
| `branchTiming` | list:54 | 100% |  |
| `branchTiming[].day` | str:46 | 85% |  |
| `branchTiming[].timing` | list:46 | 85% |  |
| `branchTiming[].timing[].endTime` | str:46 | 85% |  |
| `branchTiming[].timing[].startTime` | str:46 | 85% |  |
| `cheeziousRegion` | str:53 | 98% |  |
| `city` | str:54 | 100% |  |
| `closedBy` | str:54 | 100% |  |
| `codTaxRate` | int:54 | 100% |  |
| `complianceCheck` | bool:14 | 25% |  |
| `contactNo` | str:52 | 96% |  |
| `contactPerson` | str:50 | 92% |  |
| `createdAt` | datetime:54 | 100% |  |
| `dispatchLocks` | dict:15 | 27% |  |
| `displayTaxAtCheckout` | bool:54 | 100% |  |
| `dynamicBranchId` | str:48 | 88% |  |
| `dynamicsConfiguration` | dict:1 | 1% |  |
| `dynamicsConfiguration.apiUrl` | str:1 | 1% |  |
| `dynamicsConfiguration.cheeseType` | str:1 | 1% |  |
| `dynamicsConfiguration.enableIntegration` | bool:1 | 1% |  |
| `dynamicsConfiguration.storeId` | str:1 | 1% |  |
| `enablePickup` | bool:44 | 81% |  |
| `facilities` | list:54 | 100% |  |
| `fixedCompensation` | int:54 | 100% |  |
| `fullTimeShifts` | bool:43 | 79% |  |
| `fullTimeShiftsDuration` | int:43 | 79% |  |
| `geoMode` | str:37 | 68% |  |
| `h3TravelTime` | int:17 | 31% |  |
| `holdDateAndTime` | datetime:1, NoneType:9 | 16% |  |
| `holdTime` | int:10 | 18% |  |
| `inactiveReason` | str:52, NoneType:2 | 96% |  |
| `isAvailable` | bool:54 | 100% |  |
| `isCardMachineEnabled` | bool:1 | 1% | yes? |
| `isTaxExclusive` | bool:54 | 100% |  |
| `isVisible` | bool:53 | 98% |  |
| `location` | dict:54 | 100% | yes? |
| `location.coordinates` | list:54 | 100% |  |
| `location.type` | str:54 | 100% |  |
| `manualFullTimeShiftsDuration` | int:11 | 20% |  |
| `manualPartTimeShiftsDuration` | int:11 | 20% |  |
| `manualShiftsCreate` | bool:14 | 25% |  |
| `maxCompensation` | int:51 | 94% |  |
| `maxOrderAmount` | int:50 | 92% |  |
| `minOrderAmount` | int:54 | 100% |  |
| `minutesPerKm` | int:37 | 68% |  |
| `multiDispatch` | bool:53 | 98% |  |
| `name` | str:54 | 100% | yes? |
| `oneLegAmount` | int:54 | 100% |  |
| `onlineTaxRate` | int:54 | 100% |  |
| `partTimeShifts` | bool:14 | 25% |  |
| `partTimeShiftsDuration` | int:14 | 25% |  |
| `perOrderAmount` | int:54 | 100% |  |
| `pickupOrderReadyTime` | int:6 | 11% |  |
| `promisedBufferTime` | int:12 | 22% |  |
| `radius` | int:54 | 100% |  |
| `region` | str:54 | 100% |  |
| `riderAppEnabled` | bool:53 | 98% |  |
| `riderBreaks` | list:4 | 7% |  |
| `riderComplianceTimeInMins` | int:17 | 31% |  |
| `riderShiftLimit` | int:43 | 79% |  |
| `shiftAssignThreshold` | int:12 | 22% |  |
| `shiftCreateThreshold` | int:11 | 20% |  |
| `shiftEndBuffer` | int:12 | 22% |  |
| `shiftReflectDate` | datetime:35, NoneType:8 | 64% |  |
| `shiftStartBuffer` | int:14 | 25% |  |
| `specialTiming` | list:54 | 100% |  |
| `specialTiming[].date` | str:1 | 1% |  |
| `specialTiming[].endTime` | str:1 | 1% |  |
| `specialTiming[].startTime` | str:1 | 1% |  |
| `status` | str:54 | 100% |  |
| `updatedAt` | datetime:54 | 100% |  |

Sample (PII-like fields masked):
```
{'_id': '6509793557211fb674f199d4', 'name': '<masked>', 'address': '<masked>', 'city': 'Islamabad', 'region': 'Islamabad', 'facilities': ['DELIVERY', 'PICK-UP'], 'adminIds': ['8fed5add-dac3-4df9-8890-227760abd2ea'], 'status': 'INACTIVE', 'isAvailable': 'True', 'autoApprove': 'False', 'autoDispatch': 'False', 'perOrderAmount': '0', 'oneLegAmount': '0', 'bothLegAmount': '8', 'branchTiming': [{'day': 'MONDAY', 'timing': [{'startTime': '11:00am', 'endTime': '11:59pm'}, {'startTime': '12:00am', 'endTime': '03:05am'}]}, {'day': 'TUESDAY', 'timing': [{'startTime': '11:00am', 'endTime': '11:59pm'}, {'startTime': '12:00am', 'endTime': '03:05am'}]}, {'day': 'WEDNESDAY', 'timing': [{'startTime': '11:00am', 'endTime': '11:59pm'}, {'startTime': '12:00am', 'endTime': '03:05am'}]}, '...'], 'specialTiming': [], 'location': {'type': 'Point', 'coordinates': ['72.96390100530076', '33.64609801164013']}, 'createdAt': '2023-09-19 10:34:29.646000', 'updatedAt': '2026-10-11 02:25:32.278000', '__v': '0', 'cheeziousRegion': 'North', 'contactNo': '+923325355899', 'contactPerson': 'Nasir', 'radius': '5', 'fixedCompensation': '20', 'branchPrepTime': '45', 'riderAppEnabled': 'True', 'autoPublishTime': 'None', 'inactiveReason': 'Heavy Rain', 'maxCompensation': '200', 'isVisible': 'True', 'minOrderAmount': '50', 'multiDispatch': 'False', 'codTaxRate': '15', 'isTaxExclusive': 'False', 'onlineTaxRate': '5', 'autoReopenTime': '5', 'branchHexLimit': '2', 'branchHexResolution': '9', 'maxOrderAmount': '15000', 'closedBy': 'ADMIN', 'autoCloseTime': '200', 'fullTimeShifts': 'True', 'fullTimeShiftsDuration': '9', 'riderShiftLimit': '200', 'shiftReflectDate': '2024-12-02 00:00:00', 'displayTaxAtCheckout': 'True', 'allow24HoursShifts': 'True', 'dynamicBranchId': '', 'enablePickup': 'True', 'geoMode': 'hex', 'minutesPerKm': '3'}
```

## product.productassociations  (5264 docs)

| field | types (in sample) | present | PII? |
|---|---|---|---|
| `__v` | int:200 | 100% |  |
| `_id` | ObjectId:200 | 100% |  |
| `addOnsIds` | list:200 | 100% |  |
| `autoPublishTime` | NoneType:200 | 100% |  |
| `branchId` | ObjectId:200 | 100% |  |
| `createdAt` | datetime:200 | 100% |  |
| `dealPrice` | NoneType:190 | 95% |  |
| `isAvailable` | bool:200 | 100% |  |
| `isBestSeller` | bool:10 | 5% |  |
| `isDeleted` | bool:200 | 100% |  |
| `isOriginalPrice` | bool:200 | 100% |  |
| `options` | list:200 | 100% |  |
| `options[].isMultiSelectable` | bool:200 | 100% |  |
| `options[].isRequired` | bool:200 | 100% |  |
| `options[].title` | str:200 | 100% |  |
| `options[].variations` | list:200 | 100% |  |
| `options[].variations[].SKU` | str:200 | 100% |  |
| `options[].variations[].addOnsIds` | list:200 | 100% |  |
| `options[].variations[].addOnsIds[].id` | str:200 | 100% |  |
| `options[].variations[].addOnsIds[].isRequired` | bool:200 | 100% |  |
| `options[].variations[].addOnsIds[].limit` | int:200 | 100% |  |
| `options[].variations[].isAvailable` | bool:200 | 100% |  |
| `options[].variations[].name` | str:200 | 100% | yes? |
| `options[].variations[].price` | int:200 | 100% |  |
| `price` | int:200 | 100% |  |
| `productId` | str:200 | 100% |  |
| `updatedAt` | datetime:200 | 100% |  |

Sample (PII-like fields masked):
```
{'_id': '6579d70d1659fb303c233d0d', 'productId': '6579d70d1659fb303c233d03', 'branchId': '6509798d57211fb674f199e5', 'isAvailable': 'True', 'price': '690', 'isOriginalPrice': 'True', 'autoPublishTime': 'None', 'options': [{'variations': [{'addOnsIds': [{'id': '6579d09c1659fb303c233baf', 'limit': '0', 'isRequired': 'False'}], 'isAvailable': 'True', 'price': '690', 'name': '<masked>', 'SKU': '1'}, {'addOnsIds': [{'id': '6579d0ed1659fb303c233bbc', 'limit': '0', 'isRequired': 'False'}], 'isAvailable': 'True', 'price': '1350', 'name': '<masked>', 'SKU': '2'}, {'addOnsIds': [{'id': '6579d1161659fb303c233bc9', 'limit': '0', 'isRequired': 'False'}], 'isAvailable': 'True', 'price': '1850', 'name': '<masked>', 'SKU': '3'}, '...'], 'title': 'Variation', 'isMultiSelectable': 'False', 'isRequired': 'True'}], 'addOnsIds': [], 'isDeleted': 'False', 'createdAt': '2023-12-13 16:08:45.399000', 'updatedAt': '2025-01-04 17:20:33.839000', '__v': '0', 'dealPrice': 'None'}
```

## product.categoryassociations  (1657 docs)

| field | types (in sample) | present | PII? |
|---|---|---|---|
| `IsTimeSpecific` | bool:200 | 100% |  |
| `__v` | int:200 | 100% |  |
| `_id` | ObjectId:200 | 100% |  |
| `autoPublishTime` | datetime:109, NoneType:91 | 54% |  |
| `branchId` | ObjectId:200 | 100% |  |
| `categoryId` | str:200 | 100% |  |
| `createdAt` | datetime:200 | 100% |  |
| `isAvailable` | bool:200 | 100% |  |
| `isDeleted` | bool:200 | 100% |  |
| `priority` | int:200 | 100% |  |
| `publishEndTime` | NoneType:200 | 100% |  |
| `publishStartTime` | NoneType:200 | 100% |  |
| `updatedAt` | datetime:200 | 100% |  |

Sample (PII-like fields masked):
```
{'_id': '6579b8531659fb303c2333d3', 'categoryId': '6579b8531659fb303c2333c5', 'branchId': '6509816257211fb674f19bcc', 'isAvailable': 'True', 'IsTimeSpecific': 'False', 'autoPublishTime': '2024-02-18 03:35:00', 'publishStartTime': 'None', 'publishEndTime': 'None', 'priority': '29', 'isDeleted': 'False', 'createdAt': '2023-12-13 13:57:39.425000', 'updatedAt': '2026-07-10 11:43:31.520000', '__v': '0'}
```

## product.productcategoryassociations  (14 docs)

| field | types (in sample) | present | PII? |
|---|---|---|---|
| `__v` | int:14 | 100% |  |
| `_id` | ObjectId:14 | 100% |  |
| `branchId` | ObjectId:14 | 100% |  |
| `categoryId` | str:14 | 100% |  |
| `createdAt` | datetime:14 | 100% |  |
| `priority` | int:14 | 100% |  |
| `productId` | str:14 | 100% |  |
| `updatedAt` | datetime:14 | 100% |  |

Sample (PII-like fields masked):
```
{'_id': '6ac38151fded46f4697e5670', 'branchId': '6509a6ee57211fb674f1a2c4', 'categoryId': '699c22318faf67aab93bec1e', 'productId': '699c44518faf67aab93c069e', '__v': '0', 'createdAt': '2026-10-05 10:52:00.945000', 'priority': '1', 'updatedAt': '2026-10-05 10:52:00.945000'}
```

## product.branchspecificconfigurations  (54 docs)

| field | types (in sample) | present | PII? |
|---|---|---|---|
| `__v` | int:54 | 100% |  |
| `_id` | ObjectId:54 | 100% |  |
| `branchId` | ObjectId:54 | 100% |  |
| `cardMachine` | dict:54 | 100% | yes? |
| `cardMachine._id` | ObjectId:46 | 85% |  |
| `cardMachine.isAvailable` | bool:54 | 100% |  |
| `cardMachine.label` | str:46 | 85% |  |
| `cardMachine.paymentType` | str:46 | 85% |  |
| `cashOnDelivery` | dict:50 | 92% |  |
| `cashOnDelivery._id` | ObjectId:50 | 92% |  |
| `cashOnDelivery.isAvailable` | bool:50 | 92% |  |
| `cashOnDelivery.label` | str:50 | 92% |  |
| `cashOnDelivery.paymentType` | str:50 | 92% |  |
| `cashOnPickup` | dict:49 | 90% |  |
| `cashOnPickup._id` | ObjectId:49 | 90% |  |
| `cashOnPickup.isAvailable` | bool:49 | 90% |  |
| `cashOnPickup.label` | str:49 | 90% |  |
| `cashOnPickup.paymentType` | str:49 | 90% |  |
| `createdAt` | datetime:54 | 100% |  |
| `dynamicsConfiguration` | dict:5, NoneType:39 | 72% |  |
| `dynamicsConfiguration.apiUrl` | str:5 | 9% |  |
| `dynamicsConfiguration.cashTaxGroup` | str:5 | 9% |  |
| `dynamicsConfiguration.cheeseType` | str:5 | 9% |  |
| `dynamicsConfiguration.cheeziousRegion` | str:3 | 5% |  |
| `dynamicsConfiguration.enableIntegration` | bool:5 | 9% |  |
| `dynamicsConfiguration.onlineTaxGroup` | str:5 | 9% |  |
| `dynamicsConfiguration.storeId` | str:5 | 9% |  |
| `pgHbl` | dict:50 | 92% |  |
| `pgHbl._id` | ObjectId:50 | 92% |  |
| `pgHbl.isAvailable` | bool:50 | 92% |  |
| `pgHbl.label` | str:50 | 92% |  |
| `pgHbl.paymentType` | str:50 | 92% |  |
| `riderEasypaisaQR` | dict:50 | 92% |  |
| `riderEasypaisaQR._id` | ObjectId:50 | 92% |  |
| `riderEasypaisaQR.isAvailable` | bool:50 | 92% |  |
| `riderEasypaisaQR.label` | str:50 | 92% |  |
| `riderEasypaisaQR.paymentType` | str:50 | 92% |  |
| `riderJCQR` | dict:50 | 92% |  |
| `riderJCQR._id` | ObjectId:50 | 92% |  |
| `riderJCQR.isAvailable` | bool:50 | 92% |  |
| `riderJCQR.label` | str:50 | 92% |  |
| `riderJCQR.paymentType` | str:50 | 92% |  |
| `showPaymentMethodPrice` | bool:53 | 98% |  |
| `transferBranchConfiguration` | dict:12, NoneType:8 | 22% |  |
| `transferBranchConfiguration.branches` | list:12 | 22% |  |
| `updatedAt` | datetime:54 | 100% |  |
| `xpayCard` | dict:54 | 100% | yes? |
| `xpayCard._id` | ObjectId:49 | 90% |  |
| `xpayCard.isAvailable` | bool:54 | 100% |  |
| `xpayCard.label` | str:49 | 90% |  |
| `xpayCard.paymentType` | str:49 | 90% |  |
| `xpayEasypaisa` | dict:50 | 92% |  |
| `xpayEasypaisa._id` | ObjectId:49 | 90% |  |
| `xpayEasypaisa.isAvailable` | bool:50 | 92% |  |
| `xpayEasypaisa.label` | str:49 | 90% |  |
| `xpayEasypaisa.paymentType` | str:49 | 90% |  |
| `xpayJazzcash` | dict:49 | 90% |  |
| `xpayJazzcash._id` | ObjectId:49 | 90% |  |
| `xpayJazzcash.isAvailable` | bool:49 | 90% |  |
| `xpayJazzcash.label` | str:49 | 90% |  |
| `xpayJazzcash.paymentType` | str:49 | 90% |  |

Sample (PII-like fields masked):
```
{'_id': '67a46066edce96e4ddc3d4bb', 'riderJCQR': {'isAvailable': 'True', 'paymentType': 'ONLINE', 'label': 'JAZZ_CASH_DQR', '_id': '6a0eadd74c594d944c9dd152'}, 'cashOnDelivery': {'isAvailable': 'True', 'paymentType': 'COD', 'label': 'COD', '_id': '6a0eadd74c594d944c9dd153'}, 'branchId': '65097dc357211fb674f19ae5', 'createdAt': '2025-02-06 07:10:30.751000', 'updatedAt': '2026-10-08 13:22:30.208000', '__v': '0', 'cardMachine': {'isAvailable': 'True', 'paymentType': 'ONLINE', 'label': 'CARD_MACHINE', '_id': '6a0eadd74c594d944c9dd15a'}, 'cashOnPickup': {'isAvailable': 'True', 'paymentType': 'COP', 'label': 'COP', '_id': '6a0eadd74c594d944c9dd154'}, 'pgHbl': {'isAvailable': 'False', 'paymentType': 'ONLINE', 'label': 'PAYMENT_GATEWAY_HBL', '_id': '6a0eadd74c594d944c9dd155'}, 'riderEasypaisaQR': {'isAvailable': 'False', 'paymentType': 'ONLINE', 'label': 'EASYPAISA_DQR', '_id': '6a0eadd74c594d944c9dd156'}, 'showPaymentMethodPrice': 'False', 'xpayCard': {'isAvailable': 'True', 'paymentType': 'ONLINE', 'label': 'XPAY_CARD', '_id': '6a0eadd74c594d944c9dd157'}, 'xpayEasypaisa': {'isAvailable': 'True', 'paymentType': 'ONLINE', 'label': 'XPAY_EASYPAISA', '_id': '6a0eadd74c594d944c9dd159'}, 'xpayJazzcash': {'isAvailable': 'False', 'paymentType': 'ONLINE', 'label': 'XPAY_JAZZCASH', '_id': '6a0eadd74c594d944c9dd158'}}
```

## aggregator.faqs  (10 docs)

| field | types (in sample) | present | PII? |
|---|---|---|---|
| `__v` | int:10 | 100% |  |
| `_id` | str:10 | 100% |  |
| `adminId` | str:10 | 100% |  |
| `answer` | str:10 | 100% |  |
| `createdAt` | datetime:10 | 100% |  |
| `priority` | int:10 | 100% |  |
| `question` | str:10 | 100% |  |
| `status` | str:10 | 100% |  |
| `type` | str:10 | 100% |  |
| `updatedAt` | datetime:10 | 100% |  |

Sample (PII-like fields masked):
```
{'_id': '59b2c260-49f8-40d2-b1d6-d28a23fb1e3d', 'question': "What's your name?", 'answer': 'My name is customer app', 'type': 'CUSTOMER', 'priority': '12', 'status': 'UNPUBLISH', 'adminId': '3ce288b5-5836-493c-8474-6a4ad51bc167', 'createdAt': '2024-01-15 13:56:28.803000', 'updatedAt': '2026-10-12 11:53:57.938000', '__v': '0'}
```

## order.orders  (4724 docs)

| field | types (in sample) | present | PII? |
|---|---|---|---|
| `__v` | int:200 | 100% |  |
| `_id` | ObjectId:200 | 100% |  |
| `address` | dict:200 | 100% | yes? |
| `address.addressType` | str:200 | 100% | yes? |
| `address.city` | str:186, NoneType:14 | 93% |  |
| `address.country` | str:200 | 100% |  |
| `address.lat` | float:200 | 100% | yes? |
| `address.location` | str:200 | 100% | yes? |
| `address.long` | float:200 | 100% |  |
| `address.name` | str:184 | 92% | yes? |
| `address.state` | NoneType:74, str:82 | 41% |  |
| `address.street` | str:161, NoneType:32 | 80% | yes? |
| `adminId` | str:8 | 4% |  |
| `alternatePhoneNo` | list:200 | 100% | yes? |
| `amountReceiveByRider` | int:61 | 30% |  |
| `assignedAt` | datetime:72, NoneType:128 | 64% |  |
| `autoApprove` | bool:200 | 100% |  |
| `autoDispatch` | bool:200 | 100% |  |
| `branchId` | str:200 | 100% |  |
| `cancelationNotes` | str:123 | 61% | yes? |
| `createdAt` | datetime:200 | 100% |  |
| `createdInAutoDispatch` | bool:200 | 100% |  |
| `crm_order_id` | str:104, NoneType:96 | 52% |  |
| `customerId` | str:200 | 100% |  |
| `customerName` | str:200 | 100% | yes? |
| `customerNotes` | str:200 | 100% |  |
| `customerPhoneNo` | str:200 | 100% | yes? |
| `deliveredAddress` | dict:61 | 30% | yes? |
| `deliveredAddress.lat` | float:61 | 30% | yes? |
| `deliveredAddress.long` | float:61 | 30% |  |
| `deliveredAt` | datetime:88, NoneType:112 | 56% |  |
| `deliveryPrepTime` | int:200 | 100% |  |
| `dynamicsSyncStatus` | str:197 | 98% |  |
| `firstDeliveredAttempt` | NoneType:139, datetime:61 | 69% |  |
| `freezeTime` | datetime:8, NoneType:3 | 4% |  |
| `holdTime` | int:200 | 100% |  |
| `isRefunded` | bool:200 | 100% |  |
| `isReviewed` | bool:200 | 100% |  |
| `isTaxExclusive` | bool:200 | 100% |  |
| `isVerified` | bool:155, NoneType:45 | 77% |  |
| `lateStatus` | list:200 | 100% | yes? |
| `mapLocation` | NoneType:53, dict:8 | 26% | yes? |
| `mapLocation.lat` | float:8 | 4% | yes? |
| `mapLocation.long` | float:8 | 4% |  |
| `oldLocation` | dict:61 | 30% | yes? |
| `oldLocation.lat` | float:61 | 30% | yes? |
| `oldLocation.long` | float:61 | 30% |  |
| `orderAssignedAt` | datetime:86, NoneType:114 | 57% |  |
| `orderAssignedPermanently` | bool:200 | 100% |  |
| `orderAutoRejectTime` | datetime:200 | 100% |  |
| `orderDeliveryETA` | int:200 | 100% |  |
| `orderId` | ObjectId:1 | 0% |  |
| `orderItems` | list:200 | 100% |  |
| `orderItems[].addOns` | list:66 | 33% |  |
| `orderItems[].addOns[]._id` | str:1 | 0% |  |
| `orderItems[].addOns[].type` | str:1 | 0% |  |
| `orderItems[].addOns[].variations` | list:1 | 0% |  |
| `orderItems[].addOns[].variations[].image` | str:1 | 0% |  |
| `orderItems[].addOns[].variations[].name` | str:1 | 0% | yes? |
| `orderItems[].addOns[].variations[].price` | int:1 | 0% |  |
| `orderItems[].isDeal` | bool:162 | 81% |  |
| `orderItems[].productBasePrice` | int:138 | 69% |  |
| `orderItems[].productId` | str:200 | 100% |  |
| `orderItems[].productName` | str:200 | 100% | yes? |
| `orderItems[].productOptions` | list:128 | 64% |  |
| `orderItems[].productOptions[].title` | str:89 | 44% |  |
| `orderItems[].productOptions[].variations` | list:89 | 44% |  |
| `orderItems[].productOptions[].variations[].addOns` | list:80 | 40% |  |
| `orderItems[].productOptions[].variations[].addOns[]._id` | str:32 | 16% |  |
| `orderItems[].productOptions[].variations[].addOns[].type` | str:32 | 16% |  |
| `orderItems[].productOptions[].variations[].addOns[].variations` | list:32 | 16% |  |
| `orderItems[].productOptions[].variations[].addOns[].variations[].image` | str:2 | 1% |  |
| `orderItems[].productOptions[].variations[].addOns[].variations[].name` | str:19 | 9% | yes? |
| `orderItems[].productOptions[].variations[].addOns[].variations[].price` | int:19 | 9% |  |
| `orderItems[].productOptions[].variations[].name` | str:89 | 44% | yes? |
| `orderItems[].productOptions[].variations[].price` | int:89 | 44% |  |
| `orderItems[].productOptions[].variations[].quantity` | int:89 | 44% |  |
| `orderItems[].productPrepTime` | int:200 | 100% |  |
| `orderItems[].productQuantity` | int:200 | 100% |  |
| `orderItems[].specialInstruction` | str:28 | 14% |  |
| `orderNo` | str:200 | 100% |  |
| `orderPrepTime` | int:168, float:32 | 84% |  |
| `orderReadyTime` | datetime:200 | 100% |  |
| `orderUpdatedAt` | datetime:14 | 7% |  |
| `payment` | dict:200 | 100% |  |
| `payment.enableTransaction` | bool:200 | 100% |  |
| `payment.enableVoucher` | bool:143 | 71% |  |
| `payment.enableWallet` | bool:143 | 71% |  |
| `payment.isOnline` | bool:200 | 100% |  |
| `payment.onlineTransactionId` | str:1 | 0% |  |
| `payment.type` | str:200 | 100% |  |
| `paymentMethod` | str:89 | 44% |  |
| `paymentType` | str:200 | 100% |  |
| `pickedUpAt` | NoneType:132, datetime:68 | 66% |  |
| `predictedHoldTime` | NoneType:50, datetime:36 | 25% |  |
| `prepStartAt` | datetime:1 | 0% |  |
| `promisedTime` | int:200 | 100% |  |
| `readyAtKitchen` | NoneType:130, datetime:70 | 65% |  |
| `readyForPickUpAt` | NoneType:132, datetime:68 | 66% |  |
| `reason` | str:18 | 9% |  |
| `reasonSubject` | str:11 | 5% |  |
| `reqInfo` | dict:200 | 100% |  |
| `reqInfo.clientInfo` | dict:86 | 43% |  |
| `reqInfo.clientInfo.engine` | str:72 | 36% |  |
| `reqInfo.clientInfo.engine_version` | str:72 | 36% |  |
| `reqInfo.clientInfo.family` | str:72 | 36% |  |
| `reqInfo.clientInfo.name` | str:86 | 43% | yes? |
| `reqInfo.clientInfo.short_name` | str:72 | 36% | yes? |
| `reqInfo.clientInfo.type` | str:86 | 43% |  |
| `reqInfo.clientInfo.version` | str:86 | 43% |  |
| `reqInfo.deviceInfo` | dict:89 | 44% |  |
| `reqInfo.deviceInfo.brand` | str:89 | 44% |  |
| `reqInfo.deviceInfo.id` | str:89 | 44% |  |
| `reqInfo.deviceInfo.model` | str:89 | 44% |  |
| `reqInfo.deviceInfo.type` | str:89 | 44% |  |
| `reqInfo.id` | str:200 | 100% |  |
| `reqInfo.osInfo` | dict:89 | 44% |  |
| `reqInfo.osInfo.family` | str:89 | 44% |  |
| `reqInfo.osInfo.name` | str:89 | 44% | yes? |
| `reqInfo.osInfo.platform` | str:89 | 44% | yes? |
| `reqInfo.osInfo.short_name` | str:89 | 44% | yes? |
| `reqInfo.osInfo.version` | str:89 | 44% |  |
| `reqInfo.role` | list:127, str:73 | 63% |  |
| `riderAvailableAdjusted` | bool:1 | 0% |  |
| `riderAvailableTime` | datetime:39 | 19% |  |
| `riderDueAmount` | int:200 | 100% |  |
| `riderId` | str:76, NoneType:30 | 38% |  |
| `serviceTime` | int:1 | 0% |  |
| `source` | str:200 | 100% |  |
| `status` | str:200 | 100% |  |
| `subTotal` | int:200 | 100% |  |
| `taxAmount` | int:200 | 100% |  |
| `taxRate` | int:200 | 100% |  |
| `timeToDeliver` | int:200 | 100% |  |
| `total` | int:200 | 100% |  |
| `total_discount_amount` | int:12 | 6% |  |
| `transferCount` | int:200 | 100% |  |
| `transferredAt` | NoneType:195, datetime:5 | 97% |  |
| `type` | str:200 | 100% |  |
| `unverifiedReason` | str:13, NoneType:187 | 93% |  |
| `updatedAt` | datetime:200 | 100% |  |
| `voucherId` | list:200 | 100% |  |

Sample (PII-like fields masked):
```
{'_id': '67b307b917230408687abf66', 'subTotal': '548', 'orderNo': '170225-13665', 'source': 'CRM', 'transferredAt': 'None', 'assignedAt': '2025-06-23 14:58:59.424000', 'readyAtKitchen': 'None', 'holdTime': '0', 'reqInfo': {'role': ['MenuManagement'], 'id': 'd1c07d58-af0d-4a3b-8e29-0007812cdc12'}, 'total': '630', 'autoApprove': 'False', 'autoDispatch': 'True', 'createdInAutoDispatch': 'False', 'isVerified': 'True', 'unverifiedReason': 'This order costs more than 1 Rs. Please verify with the customer before accepting.', 'deliveryPrepTime': '45', 'orderPrepTime': '15', 'promisedTime': '0', 'timeToDeliver': '0', 'orderReadyTime': '2025-02-17 10:13:09.908000', 'orderAutoRejectTime': '2025-02-17 10:06:09.908000', 'orderAssignedPermanently': 'True', 'voucherId': [], 'alternatePhoneNo': ['<masked>'], 'status': 'DELIVERED', 'payment': {'isOnline': 'False', 'type': 'COD', 'enableVoucher': 'False', 'enableTransaction': 'True', 'enableWallet': 'False'}, 'paymentType': 'COD', 'paymentMethod': 'COD', 'riderDueAmount': '630', 'customerPhoneNo': '<masked>', 'customerName': '<masked>', 'address': {'lat': '<masked>', 'long': '73.18311737956718', 'location': '<masked>', 'addressType': '<masked>', 'name': '<masked>', 'street': '<masked>', 'city': 'Islamabad', 'state': 'None', 'country': 'Pakistan'}, 'type': 'DELIVERY', 'customerNotes': '', 'cancelationNotes': '<masked>', 'orderItems': [{'productId': '6579dce91659fb303c23411c', 'productName': '<masked>', 'productPrepTime': '900', 'productQuantity': '1', 'productBasePrice': '630', 'isDeal': 'False'}], 'branchId': '6509a6ee57211fb674f1a2c4', 'customerId': '52bbb85f-7a08-40d9-897f-c952ced18f3a', 'transferCount': '0', 'orderAssignedAt': '2025-06-23 10:59:04.959000', 'readyForPickUpAt': 'None', 'pickedUpAt': 'None', 'deliveredAt': '2025-06-25 09:35:00.149000', 'firstDeliveredAttempt': 'None', 'orderDeliveryETA': '45', 'isReviewed': 'False', 'isRefunded': 'False', 'lateStatus': [], 'taxAmount': '82', 'taxRate': '15', 'isTaxExclusive': 'False', 'createdAt': '2025-02-17 09:56:09.931000', 'updatedAt': '2026-06-29 12:55:17.264000', '__v': '1', 'crm_order_id': '4f158974-7e47-45da-8910-65899dd1baa1', 'freezeTime': '2025-02-17 09:56:45.606000', 'predictedHoldTime': 'None', 'riderId': '4a6014ec-a440-4448-8e6c-e309783423e3', 'orderId': '67b307b917230408687abf66', 'prepStartAt': '2025-06-04 13:23:00.967000', 'serviceTime': '20', 'riderAvailableAdjusted': 'False', 'dynamicsSyncStatus': 'ERRORED'}
```

## customer.customers  (5734 docs)

| field | types (in sample) | present | PII? |
|---|---|---|---|
| `DND` | bool:200 | 100% |  |
| `__v` | int:3 | 1% |  |
| `_id` | str:200 | 100% |  |
| `address` | list:200 | 100% | yes? |
| `address[]._id` | str:2 | 1% |  |
| `address[].addressType` | str:2 | 1% | yes? |
| `address[].area` | NoneType:2 | 1% |  |
| `address[].city` | str:2 | 1% |  |
| `address[].country` | str:2 | 1% |  |
| `address[].createdAt` | datetime:2 | 1% |  |
| `address[].crm_address_id` | str:2 | 1% |  |
| `address[].deletedAt` | NoneType:2 | 1% |  |
| `address[].landmark` | NoneType:2 | 1% |  |
| `address[].lat` | float:2 | 1% | yes? |
| `address[].location` | str:2 | 1% | yes? |
| `address[].long` | float:2 | 1% |  |
| `address[].name` | str:2 | 1% | yes? |
| `address[].postalCode` | NoneType:2 | 1% |  |
| `address[].specialInstructions` | NoneType:2 | 1% |  |
| `address[].state` | str:2 | 1% |  |
| `address[].street` | NoneType:1, str:1 | 0% | yes? |
| `address[].updatedAt` | datetime:2 | 1% |  |
| `alternatePhoneNo` | list:200 | 100% | yes? |
| `blockCod` | bool:200 | 100% |  |
| `clientInfo` | dict:199 | 99% |  |
| `clientInfo.engine` | str:157 | 78% |  |
| `clientInfo.engine_version` | str:157 | 78% |  |
| `clientInfo.family` | str:157 | 78% |  |
| `clientInfo.name` | str:180 | 90% | yes? |
| `clientInfo.short_name` | str:157 | 78% | yes? |
| `clientInfo.type` | str:180 | 90% |  |
| `clientInfo.version` | str:180 | 90% |  |
| `comments` | NoneType:200 | 100% |  |
| `createdAt` | datetime:200 | 100% |  |
| `crm_customer_id` | str:6, NoneType:194 | 97% |  |
| `customerFav` | list:200 | 100% |  |
| `customerType` | str:200 | 100% |  |
| `deletedAt` | NoneType:200 | 100% |  |
| `deviceInfo` | dict:199 | 99% |  |
| `deviceInfo.brand` | str:199 | 99% |  |
| `deviceInfo.id` | str:199 | 99% |  |
| `deviceInfo.model` | str:199 | 99% |  |
| `deviceInfo.type` | str:199 | 99% |  |
| `dob` | datetime:5, NoneType:195 | 97% |  |
| `email` | NoneType:199, str:1 | 99% | yes? |
| `fullName` | str:6, NoneType:194 | 97% | yes? |
| `image` | NoneType:200 | 100% |  |
| `ipAddress` | str:199, NoneType:1 | 99% | yes? |
| `osInfo` | dict:199 | 99% |  |
| `osInfo.family` | str:187 | 93% |  |
| `osInfo.name` | str:187 | 93% | yes? |
| `osInfo.platform` | str:187 | 93% | yes? |
| `osInfo.short_name` | str:187 | 93% | yes? |
| `osInfo.version` | str:187 | 93% |  |
| `phoneNo` | str:6, NoneType:194 | 97% | yes? |
| `reason` | NoneType:200 | 100% |  |
| `reasonSubject` | NoneType:200 | 100% |  |
| `revenue` | int:200 | 100% |  |
| `status` | str:200 | 100% |  |
| `type` | str:5, NoneType:195 | 97% |  |
| `updatedAt` | datetime:200 | 100% |  |

Sample (PII-like fields masked):
```
{'_id': '885ad17b-86ce-4237-b92c-2d20a3b9c4a8', 'fullName': '<masked>', 'email': 'None', 'phoneNo': '<masked>', 'blockCod': 'False', 'dob': '1988-09-14 00:00:00', 'comments': 'None', 'DND': 'False', 'image': 'None', 'reason': 'None', 'reasonSubject': 'None', 'type': '', 'revenue': '0', 'status': 'ACTIVE', 'ipAddress': '<masked>', 'crm_customer_id': 'e068efaa-c315-4529-9548-33fdcf13e0f7', 'alternatePhoneNo': [], 'deviceInfo': {'id': '', 'type': 'desktop', 'brand': '', 'model': ''}, 'osInfo': {'name': '<masked>', 'version': '10', 'short_name': '<masked>', 'platform': '<masked>', 'family': 'Windows'}, 'clientInfo': {'type': 'browser', 'name': '<masked>', 'short_name': '<masked>', 'version': '154.0.0.0', 'engine': 'Blink', 'engine_version': '154.0.0.0', 'family': 'Chrome'}, 'customerType': 'Authorized', 'deletedAt': 'None', 'address': [], 'customerFav': [], 'createdAt': '2026-09-28 08:13:05.331000', 'updatedAt': '2026-09-28 09:12:13.365000', '__v': '0'}
```
