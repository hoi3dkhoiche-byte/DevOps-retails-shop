/**
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy of this
 * software and associated documentation files (the "Software"), to deal in the Software
 * without restriction, including without limitation the rights to use, copy, modify,
 * merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
 * permit persons to whom the Software is furnished to do so.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
 * INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
 * PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
 * HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
 * OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
 * SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
 */

import { Test, TestingModule } from '@nestjs/testing';
import { INestApplication } from '@nestjs/common';
import request from 'supertest';
import { AppModule } from './../src/app.module';
import { StartedTestContainer, Wait } from 'testcontainers';
import { GenericContainer } from 'testcontainers';
import { ConfigModule } from '@nestjs/config';
import { RedisCheckoutRepository } from '../src/checkout/repositories/RedisCheckoutRepository';

jest.setTimeout(120000); // Includes cold fixture pull and application bootstrap.

describe('AppController (e2e)', () => {
  let app: INestApplication;
  let redisContainer: StartedTestContainer;

  beforeAll(async () => {
    redisContainer = await new GenericContainer('redis:7.4.2-alpine')
      .withExposedPorts(6379)
      .withStartupTimeout(90000)
      .withWaitStrategy(Wait.forLogMessage('Ready to accept connections'))
      .start();

    const redisPort = redisContainer.getMappedPort(6379);
    const redisHost = redisContainer.getHost();

    const moduleFixture: TestingModule = await Test.createTestingModule({
      imports: [
        ConfigModule.forRoot({
          load: [
            () => ({
              persistence: {
                provider: 'redis',
                redis: {
                  url: `redis://${redisHost}:${redisPort}`,
                },
              },
            }),
          ],
        }),
        AppModule,
      ],
    }).compile();

    app = moduleFixture.createNestApplication();
    await app.init();
  });

  afterAll(async () => {
    if (app) {
      await app.close();
    }
    if (redisContainer) {
      await redisContainer.stop();
    }
  });

  it('/health (GET)', () => {
    return request(app.getHttpServer()).get('/health').expect(200);
  });

  it('/checkout/test123 (GET)', () => {
    return request(app.getHttpServer()).get('/checkout/test123').expect(404);
  });

  it('/checkout/test124 (POST)', () => {
    return request(app.getHttpServer())
      .post('/checkout/test124/update')
      .send(valid)
      .expect(201);
  });

  it('keeps dev and prod customer data separate on the same Redis server', async () => {
    const url = `redis://${redisContainer.getHost()}:${redisContainer.getMappedPort(6379)}`;
    const dev = new RedisCheckoutRepository(url, url, 'retail:dev:');
    const prod = new RedisCheckoutRepository(url, url, 'retail:prod:');
    try {
      await dev.set('shared-customer', 'dev-checkout');
      await prod.set('shared-customer', 'prod-checkout');
      expect(await dev.get('shared-customer')).toBe('dev-checkout');
      expect(await prod.get('shared-customer')).toBe('prod-checkout');
      await dev.remove('shared-customer');
      expect(await dev.get('shared-customer')).toBeNull();
      expect(await prod.get('shared-customer')).toBe('prod-checkout');
    } finally {
      await dev.onModuleDestroy();
      await prod.onModuleDestroy();
    }
  });
});

const valid = {
  customerEmail: 'asdasd@asdasd.com',
  items: [
    {
      id: 'a1',
      name: 'A1',
      quantity: 1,
      unitCost: 123,
    },
    {
      id: 'b1',
      name: 'B1',
      quantity: 1,
      unitCost: 123,
    },
  ],
  shippingAddress: {
    firstName: 'John',
    lastName: 'Doe',
    address1: '999 Main St.',
    address2: '#123',
    city: 'Sometown',
    state: 'AB',
    zip: '12345',
  },
  subtotal: 492,
};
